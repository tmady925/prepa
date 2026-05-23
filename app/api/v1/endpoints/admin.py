from fastapi import APIRouter, Request, Depends, HTTPException, Header
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.db.database import get_db
from app.core.settings import get_settings
from app.models.user import User
from app.models.subscription import Subscription
from app.models.message import Message
from app.services.config_service import config_service, DEFAULTS

settings = get_settings()
router = APIRouter()


def verify_admin(x_admin_key: str = Header(None)):
    if x_admin_key != settings.admin_secret_key:
        raise HTTPException(status_code=401, detail="Non autorisé")
    return True


# ── API JSON ──────────────────────────────────────────────────────────

@router.get("/admin/stats")
async def get_stats(
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    total_users = await db.scalar(select(func.count(User.id)))
    active_users = await db.scalar(select(func.count(User.id)).where(User.status == "active"))
    pro_users = await db.scalar(select(func.count(User.id)).where(User.plan == "pro"))
    total_messages = await db.scalar(select(func.sum(User.total_messages)))
    total_revenue = await db.scalar(select(func.sum(Subscription.amount_fcfa)).where(Subscription.status == "active"))

    return {
        "total_users": total_users or 0,
        "active_users": active_users or 0,
        "pro_users": pro_users or 0,
        "free_users": (active_users or 0) - (pro_users or 0),
        "total_messages": total_messages or 0,
        "total_revenue_fcfa": total_revenue or 0,
        "conversion_rate": round((pro_users or 0) / max(active_users or 1, 1) * 100, 1),
    }


@router.get("/admin/users")
async def get_users(
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
    limit: int = 50,
    offset: int = 0,
):
    result = await db.execute(
        select(User)
        .order_by(User.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    users = result.scalars().all()
    return [
        {
            "id": str(u.id),
            "phone": u.phone_number,
            "name": u.name,
            "plan": u.plan,
            "status": u.status,
            "exam_type": u.exam_type,
            "series": u.series,
            "streak_days": u.streak_days,
            "total_messages": u.total_messages,
            "engagement_score": u.engagement_score,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        }
        for u in users
    ]


@router.get("/admin/config")
async def get_config(
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    from app.models.config import PlatformConfig
    result = await db.execute(select(PlatformConfig).order_by(PlatformConfig.key))
    configs = result.scalars().all()

    config_list = []
    for key, default in DEFAULTS.items():
        db_val = next((c.value for c in configs if c.key == key and c.scope == "global"), None)
        config_list.append({
            "key": key,
            "value": db_val if db_val is not None else default,
            "default": default,
            "is_custom": db_val is not None,
        })

    return config_list


@router.post("/admin/config")
async def update_config(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    data = await request.json()
    key = data.get("key")
    value = data.get("value")

    if not key or value is None:
        raise HTTPException(status_code=400, detail="key et value requis")

    await config_service.set(key, value, updated_by="admin")
    return {"status": "ok", "key": key, "value": value}


@router.post("/admin/users/{user_id}/reset-quota")
async def reset_quota(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    from sqlalchemy import update
    import uuid
    await db.execute(
        update(User)
        .where(User.id == uuid.UUID(user_id))
        .values(daily_messages_used=0, daily_messages_bonus=100)
    )
    await db.commit()
    return {"status": "ok"}


@router.post("/admin/users/{user_id}/activate-pro")
async def activate_pro_admin(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    import uuid
    result = await db.execute(select(User).where(User.id == uuid.UUID(user_id)))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur non trouvé")

    from app.services.payment_service import payment_service
    await payment_service.activate_pro(db=db, user=user, paydunya_token="admin_manual")
    await db.commit()
    return {"status": "ok"}


# ── DASHBOARD HTML ────────────────────────────────────────────────────

@router.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    return HTMLResponse(content=DASHBOARD_HTML)


DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Prepa — Dashboard Admin</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f0f1a; color: #e2e8f0; min-height: 100vh; }
        .header { background: #1a1a2e; padding: 20px 32px; border-bottom: 1px solid #2d2d4e; display: flex; justify-content: space-between; align-items: center; }
        .header h1 { font-size: 22px; font-weight: 700; color: #818cf8; }
        .header .auth { display: flex; gap: 12px; align-items: center; }
        .header input { background: #2d2d4e; border: 1px solid #3d3d6e; color: #e2e8f0; padding: 8px 14px; border-radius: 8px; font-size: 14px; width: 240px; }
        .header button { background: #6366f1; color: white; border: none; padding: 8px 18px; border-radius: 8px; cursor: pointer; font-size: 14px; font-weight: 600; }
        .header button:hover { background: #4f46e5; }
        .container { max-width: 1200px; margin: 0 auto; padding: 32px; }
        .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 16px; margin-bottom: 32px; }
        .stat-card { background: #1a1a2e; border: 1px solid #2d2d4e; border-radius: 12px; padding: 20px; text-align: center; }
        .stat-card .value { font-size: 32px; font-weight: 800; color: #818cf8; }
        .stat-card .label { font-size: 12px; color: #94a3b8; margin-top: 4px; text-transform: uppercase; letter-spacing: 0.05em; }
        .section { background: #1a1a2e; border: 1px solid #2d2d4e; border-radius: 12px; padding: 24px; margin-bottom: 24px; }
        .section h2 { font-size: 16px; font-weight: 600; margin-bottom: 20px; color: #c7d2fe; }
        table { width: 100%; border-collapse: collapse; font-size: 13px; }
        th { text-align: left; padding: 10px 14px; color: #94a3b8; font-weight: 500; border-bottom: 1px solid #2d2d4e; font-size: 11px; text-transform: uppercase; }
        td { padding: 10px 14px; border-bottom: 1px solid #1e1e38; }
        tr:hover td { background: #1e1e38; }
        .badge { display: inline-block; padding: 2px 10px; border-radius: 20px; font-size: 11px; font-weight: 600; }
        .badge.pro { background: #fef3c7; color: #92400e; }
        .badge.free { background: #e0e7ff; color: #3730a3; }
        .badge.active { background: #d1fae5; color: #065f46; }
        .badge.onboarding { background: #fce7f3; color: #9d174d; }
        .btn-sm { background: #6366f1; color: white; border: none; padding: 4px 10px; border-radius: 6px; cursor: pointer; font-size: 11px; }
        .btn-sm:hover { background: #4f46e5; }
        .config-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; }
        .config-item { background: #0f0f1a; border: 1px solid #2d2d4e; border-radius: 8px; padding: 14px; display: flex; justify-content: space-between; align-items: center; gap: 12px; }
        .config-key { font-size: 12px; color: #94a3b8; font-family: monospace; }
        .config-value { display: flex; gap: 8px; align-items: center; }
        .config-value input { background: #1a1a2e; border: 1px solid #3d3d6e; color: #e2e8f0; padding: 5px 10px; border-radius: 6px; font-size: 13px; width: 100px; text-align: right; }
        .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 4px; }
        .dot.custom { background: #818cf8; }
        .dot.default { background: #4b5563; }
        .loading { color: #94a3b8; text-align: center; padding: 40px; }
        .toast { position: fixed; bottom: 24px; right: 24px; background: #10b981; color: white; padding: 12px 20px; border-radius: 10px; font-size: 14px; display: none; z-index: 1000; }
    </style>
</head>
<body>
<div class="header">
    <h1>⚡ Prepa Admin</h1>
    <div class="auth">
        <input type="password" id="adminKey" placeholder="Clé admin..." />
        <button onclick="loadDashboard()">Connexion</button>
    </div>
</div>

<div class="container">
    <div id="content" class="loading">Entrez la clé admin pour accéder au dashboard</div>
</div>

<div class="toast" id="toast"></div>

<script>
let API_KEY = '';

function showToast(msg, error = false) {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.style.background = error ? '#ef4444' : '#10b981';
    t.style.display = 'block';
    setTimeout(() => t.style.display = 'none', 3000);
}

async function api(path, method = 'GET', body = null) {
    const opts = {
        method,
        headers: { 'X-Admin-Key': API_KEY, 'Content-Type': 'application/json' }
    };
    if (body) opts.body = JSON.stringify(body);
    const res = await fetch('/api/v1' + path, opts);
    if (!res.ok) throw new Error('Erreur API');
    return res.json();
}

async function loadDashboard() {
    API_KEY = document.getElementById('adminKey').value;
    if (!API_KEY) return;

    document.getElementById('content').innerHTML = '<div class="loading">Chargement...</div>';

    try {
        const [stats, users, configs] = await Promise.all([
            api('/admin/stats'),
            api('/admin/users?limit=20'),
            api('/admin/config'),
        ]);

        document.getElementById('content').innerHTML = `
            <div class="stats-grid">
                <div class="stat-card"><div class="value">${stats.total_users}</div><div class="label">Élèves total</div></div>
                <div class="stat-card"><div class="value">${stats.active_users}</div><div class="label">Actifs</div></div>
                <div class="stat-card"><div class="value">${stats.pro_users}</div><div class="label">Pro ⭐</div></div>
                <div class="stat-card"><div class="value">${stats.conversion_rate}%</div><div class="label">Conversion</div></div>
                <div class="stat-card"><div class="value">${stats.total_messages.toLocaleString()}</div><div class="label">Messages</div></div>
                <div class="stat-card"><div class="value">${(stats.total_revenue_fcfa || 0).toLocaleString()} F</div><div class="label">Revenus</div></div>
            </div>

            <div class="section">
                <h2>⚙️ Configuration plateforme</h2>
                <div class="config-grid">
                    ${configs.map(c => `
                        <div class="config-item">
                            <div>
                                <span class="dot ${c.is_custom ? 'custom' : 'default'}"></span>
                                <span class="config-key">${c.key}</span>
                            </div>
                            <div class="config-value">
                                <input type="text" value="${c.value}" id="cfg_${c.key.replace(/[^a-z0-9]/gi, '_')}" />
                                <button class="btn-sm" onclick="saveConfig('${c.key}')">✓</button>
                            </div>
                        </div>
                    `).join('')}
                </div>
            </div>

            <div class="section">
                <h2>👥 Élèves récents</h2>
                <table>
                    <thead>
                        <tr>
                            <th>Nom</th>
                            <th>Téléphone</th>
                            <th>Plan</th>
                            <th>Statut</th>
                            <th>Examen</th>
                            <th>Streak</th>
                            <th>Messages</th>
                            <th>Score</th>
                            <th>Actions</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${users.map(u => `
                            <tr>
                                <td>${u.name || '—'}</td>
                                <td>${u.phone}</td>
                                <td><span class="badge ${u.plan}">${u.plan}</span></td>
                                <td><span class="badge ${u.status}">${u.status}</span></td>
                                <td>${u.exam_type || '—'} ${u.series || ''}</td>
                                <td>🔥 ${u.streak_days}j</td>
                                <td>${u.total_messages}</td>
                                <td>${u.engagement_score}/100</td>
                                <td style="display:flex;gap:6px">
                                    <button class="btn-sm" onclick="resetQuota('${u.id}')">Reset quota</button>
                                    ${u.plan !== 'pro' ? `<button class="btn-sm" onclick="activatePro('${u.id}')">→ Pro</button>` : ''}
                                </td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
            </div>
        `;
    } catch (e) {
        document.getElementById('content').innerHTML = '<div class="loading">Clé invalide ou erreur serveur</div>';
    }
}

async function saveConfig(key) {
    const safeKey = key.replace(/[^a-z0-9]/gi, '_');
    const val = document.getElementById('cfg_' + safeKey).value;
    let parsed = val;
    if (!isNaN(val) && val !== '') parsed = Number(val);
    if (val === 'true') parsed = true;
    if (val === 'false') parsed = false;
    try {
        await api('/admin/config', 'POST', { key, value: parsed });
        showToast(`${key} mis à jour → ${parsed}`);
    } catch (e) {
        showToast('Erreur', true);
    }
}

async function resetQuota(userId) {
    try {
        await api('/admin/users/' + userId + '/reset-quota', 'POST');
        showToast('Quota réinitialisé');
    } catch (e) {
        showToast('Erreur', true);
    }
}

async function activatePro(userId) {
    try {
        await api('/admin/users/' + userId + '/activate-pro', 'POST');
        showToast('Plan Pro activé');
        loadDashboard();
    } catch (e) {
        showToast('Erreur', true);
    }
}
</script>
</body>
</html>
"""