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
        select(User).order_by(User.created_at.desc()).limit(limit).offset(offset)
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


@router.post("/admin/documents/upload")
async def upload_document(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    """Upload et indexe un document depuis l'admin. Max 10MB."""
    from app.services.rag.indexing_service import indexing_service
    import base64

    # Vérifie la taille avant de lire
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > 15 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Fichier trop grand (max 10MB). Utilise le script local.")

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Requête invalide")

    filename = data.get("filename", "document")
    file_b64 = data.get("file_b64", "")
    title = data.get("title", filename)
    exam_type = data.get("exam_type") or None
    series = data.get("series") or None
    subject = data.get("subject") or None
    doc_type = data.get("doc_type", "cours")

    if not file_b64:
        raise HTTPException(status_code=400, detail="Fichier manquant")

    try:
        file_bytes = base64.b64decode(file_b64)
    except Exception:
        raise HTTPException(status_code=400, detail="Fichier invalide")

    # Vérifie la taille du fichier décodé
    if len(file_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Fichier trop grand (max 10MB). Utilise le script local.")

    result = await indexing_service.index_document(
        db=db,
        file_bytes=file_bytes,
        filename=filename,
        title=title,
        exam_type=exam_type,
        series=series,
        subject=subject,
        doc_type=doc_type,
        uploaded_by="admin",
    )
    return result


@router.get("/admin/documents")
async def get_documents(
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    from app.services.rag.indexing_service import indexing_service
    return await indexing_service.get_documents(db)


@router.delete("/admin/documents/{document_id}")
async def delete_document(
    document_id: str,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(verify_admin),
):
    from app.services.rag.indexing_service import indexing_service
    success = await indexing_service.delete_document(db, document_id)
    if not success:
        raise HTTPException(status_code=404, detail="Document non trouvé")
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
        .badge.indexed { background: #d1fae5; color: #065f46; }
        .badge.onboarding { background: #fce7f3; color: #9d174d; }
        .badge.processing { background: #fef3c7; color: #92400e; }
        .badge.error { background: #fee2e2; color: #991b1b; }
        .btn-sm { background: #6366f1; color: white; border: none; padding: 4px 10px; border-radius: 6px; cursor: pointer; font-size: 11px; }
        .btn-sm:hover { background: #4f46e5; }
        .btn-danger { background: #ef4444; }
        .btn-danger:hover { background: #dc2626; }
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
        select { background: #0f0f1a; border: 1px solid #3d3d6e; color: #e2e8f0; padding: 6px; border-radius: 6px; font-size: 12px; }
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
    if (!res.ok) throw new Error('Erreur API ' + res.status);
    return res.json();
}

async function loadDashboard() {
    API_KEY = document.getElementById('adminKey').value;
    if (!API_KEY) return;

    document.getElementById('content').innerHTML = '<div class="loading">Chargement...</div>';

    try {
        const [stats, users, configs, docs] = await Promise.all([
            api('/admin/stats'),
            api('/admin/users?limit=20'),
            api('/admin/config'),
            api('/admin/documents'),
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
                <h2>📚 Documents RAG</h2>
                <p style="font-size:11px;color:#94a3b8;margin-bottom:12px">Max 10MB par fichier. Pour les fichiers plus grands, utilise le script local : <code style="background:#0f0f1a;padding:2px 6px;border-radius:4px">python scripts/index_documents.py</code></p>
                <div style="margin-bottom:16px;display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap">
                    <div>
                        <div style="font-size:11px;color:#94a3b8;margin-bottom:4px">Fichier (PDF/Word)</div>
                        <input type="file" id="docFile" accept=".pdf,.docx,.doc" style="background:#0f0f1a;border:1px solid #3d3d6e;color:#e2e8f0;padding:6px;border-radius:6px;font-size:12px"/>
                    </div>
                    <div>
                        <div style="font-size:11px;color:#94a3b8;margin-bottom:4px">Titre</div>
                        <input type="text" id="docTitle" placeholder="Titre du document" style="background:#0f0f1a;border:1px solid #3d3d6e;color:#e2e8f0;padding:6px 10px;border-radius:6px;font-size:12px;width:160px"/>
                    </div>
                    <div>
                        <div style="font-size:11px;color:#94a3b8;margin-bottom:4px">Examen</div>
                        <select id="docExam">
                            <option value="">-- Examen --</option>
                            <option value="bac_senegal">BAC Sénégal</option>
                            <option value="bfem">BFEM</option>
                            <option value="concours">Concours</option>
                        </select>
                    </div>
                    <div>
                        <div style="font-size:11px;color:#94a3b8;margin-bottom:4px">Série</div>
                        <select id="docSeries">
                            <option value="">-- Série --</option>
                            <option value="S1">S1</option>
                            <option value="S2">S2</option>
                            <option value="S3">S3</option>
                            <option value="L1">L1</option>
                            <option value="L2">L2</option>
                            <option value="T">T</option>
                        </select>
                    </div>
                    <div>
                        <div style="font-size:11px;color:#94a3b8;margin-bottom:4px">Matière</div>
                        <select id="docSubject">
                            <option value="">-- Matière --</option>
                            <option value="maths">Maths</option>
                            <option value="physique">Physique</option>
                            <option value="svt">SVT</option>
                            <option value="francais">Français</option>
                            <option value="philosophie">Philosophie</option>
                            <option value="histoire_geo">Histoire-Géo</option>
                            <option value="anglais">Anglais</option>
                        </select>
                    </div>
                    <div>
                        <div style="font-size:11px;color:#94a3b8;margin-bottom:4px">Type</div>
                        <select id="docType">
                            <option value="cours">Cours</option>
                            <option value="annale">Annale</option>
                            <option value="fiche">Fiche</option>
                            <option value="exercice">Exercice</option>
                        </select>
                    </div>
                    <button class="btn-sm" onclick="uploadDocument()" style="padding:8px 16px">⬆️ Indexer</button>
                </div>
                <div id="uploadStatus" style="font-size:12px;color:#94a3b8;margin-bottom:12px"></div>
                <table>
                    <thead>
                        <tr>
                            <th>Titre</th>
                            <th>Examen</th>
                            <th>Série</th>
                            <th>Matière</th>
                            <th>Type</th>
                            <th>Pages</th>
                            <th>Chunks</th>
                            <th>Statut</th>
                            <th>Action</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${docs.map(d => `
                            <tr>
                                <td>${d.title}</td>
                                <td>${d.exam_type || '—'}</td>
                                <td>${d.series || '—'}</td>
                                <td>${d.subject || '—'}</td>
                                <td>${d.doc_type || '—'}</td>
                                <td>${d.page_count}</td>
                                <td>${d.chunk_count}</td>
                                <td><span class="badge ${d.status}">${d.status}</span></td>
                                <td><button class="btn-sm btn-danger" onclick="deleteDocument('${d.id}')">🗑</button></td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
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
        showToast(key + ' mis à jour');
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

async function uploadDocument() {
    const file = document.getElementById('docFile').files[0];
    if (!file) { showToast('Sélectionne un fichier', true); return; }

    if (file.size > 10 * 1024 * 1024) {
        showToast('Fichier trop grand (max 10MB). Utilise le script local.', true);
        document.getElementById('uploadStatus').textContent = '❌ Fichier trop grand (max 10MB). Utilise : python scripts/index_documents.py';
        return;
    }

    const status = document.getElementById('uploadStatus');
    status.textContent = '⏳ Lecture du fichier (' + (file.size / 1024 / 1024).toFixed(1) + ' MB)...';

    const reader = new FileReader();
    reader.onload = async (e) => {
        const b64 = e.target.result.split(',')[1];
        status.textContent = '⏳ Indexation en cours... (peut prendre 2-3 minutes)';

        try {
            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), 300000);

            const res = await fetch('/api/v1/admin/documents/upload', {
                method: 'POST',
                headers: {
                    'X-Admin-Key': API_KEY,
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    filename: file.name,
                    file_b64: b64,
                    title: document.getElementById('docTitle').value || file.name,
                    exam_type: document.getElementById('docExam').value,
                    series: document.getElementById('docSeries').value,
                    subject: document.getElementById('docSubject').value,
                    doc_type: document.getElementById('docType').value,
                }),
                signal: controller.signal,
            });

            clearTimeout(timeoutId);

            if (res.status === 413) {
                status.textContent = '❌ Fichier trop grand. Utilise le script local.';
                showToast('Fichier trop grand', true);
                return;
            }

            if (!res.ok) throw new Error('Erreur serveur ' + res.status);
            const result = await res.json();

            if (result.success) {
                status.textContent = '✅ Indexé: ' + result.chunks + ' chunks, ' + result.pages + ' pages';
                showToast('Document indexé avec succès');
                loadDashboard();
            } else {
                status.textContent = '❌ Erreur: ' + result.error;
                showToast('Erreur indexation', true);
            }
        } catch(e) {
            if (e.name === 'AbortError') {
                status.textContent = '❌ Timeout — fichier trop complexe, utilise le script local';
            } else {
                status.textContent = '❌ Erreur: ' + e.message;
            }
            showToast('Erreur', true);
        }
    };
    reader.readAsDataURL(file);
}

async function deleteDocument(docId) {
    if (!confirm('Supprimer ce document et tous ses chunks ?')) return;
    try {
        await api('/admin/documents/' + docId, 'DELETE');
        showToast('Document supprimé');
        loadDashboard();
    } catch(e) {
        showToast('Erreur', true);
    }
}
</script>
</body>
</html>
"""