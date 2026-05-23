"""
Générateur d'images pédagogiques pour WhatsApp.
Supporte : formules, graphes, tableaux, chronologies, schémas.
"""
import io
import re
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib import rcParams
from sympy import symbols, sympify, lambdify
from PIL import Image

# Style global
rcParams['font.family'] = 'DejaVu Sans'
rcParams['figure.facecolor'] = '#FFFFFF'
rcParams['axes.facecolor'] = '#F8F9FA'


class ImageGenerator:

    def _clean_latex(self, formula: str) -> str:
        """Nettoie une formule LaTeX avant rendu."""
        # Supprime les délimiteurs $ résiduels
        formula = re.sub(r'^\$+|\$+$', '', formula.strip())
        # Supprime \( \) résiduels
        formula = re.sub(r'^\\\(|\\\)$', '', formula.strip())
        # Supprime \[ \] résiduels
        formula = re.sub(r'^\\\[|\\\]$', '', formula.strip())
        return formula.strip()

    # ── Formules mathématiques ─────────────────────────────────────────

    async def formula_to_image(self, latex_formulas: list[str], title: str = "") -> bytes:
        """Génère une image propre avec une ou plusieurs formules LaTeX."""
        # Nettoie les formules
        cleaned = []
        for f in latex_formulas:
            f = self._clean_latex(f)
            if f:
                cleaned.append(f)
        if not cleaned:
            cleaned = [""]
        latex_formulas = cleaned

        n = len(latex_formulas)
        fig_height = max(2, 1.2 * n + (0.8 if title else 0))
        fig, ax = plt.subplots(figsize=(8, fig_height))
        ax.axis('off')

        if title:
            ax.text(0.5, 0.95, title, transform=ax.transAxes,
                    fontsize=14, fontweight='bold', ha='center', va='top',
                    color='#1a1a2e')

        y_start = 0.85 if title else 0.92
        step = 1.0 / (n + 1)

        for i, formula in enumerate(latex_formulas):
            y = y_start - i * step * 1.5
            ax.add_patch(mpatches.FancyBboxPatch(
                (0.05, y - 0.08), 0.9, 0.18,
                boxstyle="round,pad=0.02",
                facecolor='#EEF2FF', edgecolor='#6366F1', linewidth=1.5,
                transform=ax.transAxes
            ))
            try:
                ax.text(0.5, y, f"${formula}$",
                        transform=ax.transAxes,
                        fontsize=16, ha='center', va='center',
                        color='#1e1b4b')
            except Exception:
                # Fallback texte simple si LaTeX invalide
                ax.text(0.5, y, formula,
                        transform=ax.transAxes,
                        fontsize=14, ha='center', va='center',
                        color='#1e1b4b')

        plt.tight_layout()
        return self._fig_to_bytes(fig)

    # ── Graphe de fonction ─────────────────────────────────────────────

    async def plot_function(
        self,
        expressions: list[str],
        x_range: tuple = (-5, 5),
        title: str = "",
        labels: list[str] = None,
    ) -> bytes:
        """Génère le graphe d'une ou plusieurs fonctions."""
        x = symbols('x')
        fig, ax = plt.subplots(figsize=(8, 5))

        colors = ['#6366F1', '#EF4444', '#10B981', '#F59E0B', '#3B82F6']
        x_vals = np.linspace(x_range[0], x_range[1], 500)

        for i, expr_str in enumerate(expressions):
            try:
                expr = sympify(expr_str)
                f = lambdify(x, expr, 'numpy')
                y_vals = f(x_vals)
                y_vals = np.where(np.abs(y_vals) > 100, np.nan, y_vals)
                label = labels[i] if labels and i < len(labels) else f"f{i+1}(x) = {expr_str}"
                ax.plot(x_vals, y_vals, color=colors[i % len(colors)],
                        linewidth=2.5, label=label)
            except Exception as e:
                print(f"Erreur graphe {expr_str}: {e}")

        ax.axhline(y=0, color='#374151', linewidth=1, linestyle='-')
        ax.axvline(x=0, color='#374151', linewidth=1, linestyle='-')
        ax.grid(True, alpha=0.3, color='#9CA3AF')
        ax.legend(fontsize=11, loc='best')
        ax.set_xlabel('x', fontsize=12)
        ax.set_ylabel('f(x)', fontsize=12)
        if title:
            ax.set_title(title, fontsize=14, fontweight='bold', color='#1a1a2e', pad=15)

        plt.tight_layout()
        return self._fig_to_bytes(fig)

    # ── Tableau de variations ──────────────────────────────────────────

    async def variation_table(
        self,
        x_values: list,
        fx_values: list,
        signs: list[str],
        title: str = "Tableau de variations",
    ) -> bytes:
        fig, ax = plt.subplots(figsize=(10, 3.5))
        ax.axis('off')

        n_cols = len(x_values)

        sign_row = ["f'(x)", " "]
        for s in signs:
            sign_row.append(s)
            sign_row.append(" ")

        rows = [
            ["x"] + [str(v) for v in x_values],
            sign_row[:n_cols + 1],
            ["f(x)"] + [str(v) for v in fx_values],
        ]

        table = ax.table(cellText=rows, loc='center', cellLoc='center')
        table.auto_set_font_size(False)
        table.set_fontsize(13)
        table.scale(1.2, 2.5)

        for (row, col), cell in table.get_celld().items():
            if col == 0:
                cell.set_facecolor('#6366F1')
                cell.set_text_props(color='white', fontweight='bold')
            elif row == 0:
                cell.set_facecolor('#EEF2FF')
                cell.set_text_props(fontweight='bold', color='#1e1b4b')
            else:
                cell.set_facecolor('#F8F9FF')
            cell.set_edgecolor('#C7D2FE')

        ax.set_title(title, fontsize=14, fontweight='bold', color='#1a1a2e', pad=20)
        plt.tight_layout()
        return self._fig_to_bytes(fig)

    # ── Tableau de données ─────────────────────────────────────────────

    async def data_table(
        self,
        headers: list[str],
        rows: list[list],
        title: str = "",
    ) -> bytes:
        fig_height = max(2.5, 0.5 * len(rows) + 1.5)
        fig, ax = plt.subplots(figsize=(10, fig_height))
        ax.axis('off')

        table = ax.table(
            cellText=rows,
            colLabels=headers,
            loc='center',
            cellLoc='center',
        )
        table.auto_set_font_size(False)
        table.set_fontsize(12)
        table.scale(1.2, 1.8)

        for (row, col), cell in table.get_celld().items():
            if row == 0:
                cell.set_facecolor('#6366F1')
                cell.set_text_props(color='white', fontweight='bold')
            elif row % 2 == 0:
                cell.set_facecolor('#F3F4FF')
            else:
                cell.set_facecolor('#FFFFFF')
            cell.set_edgecolor('#E0E7FF')

        if title:
            ax.set_title(title, fontsize=14, fontweight='bold', color='#1a1a2e', pad=15)

        plt.tight_layout()
        return self._fig_to_bytes(fig)

    # ── Chronologie ────────────────────────────────────────────────────

    async def timeline(self, events: list[dict], title: str = "Chronologie") -> bytes:
        n = len(events)
        fig_height = max(4, 0.8 * n + 2)
        fig, ax = plt.subplots(figsize=(10, fig_height))
        ax.axis('off')

        ax.plot([0.5, 0.5], [0.05, 0.95], color='#6366F1',
                linewidth=3, transform=ax.transAxes, zorder=1)

        step = 0.85 / max(n, 1)

        for i, event in enumerate(events):
            y = 0.9 - i * step
            side = "left" if i % 2 == 0 else "right"
            ax.plot(0.5, y, 'o', color='#6366F1', markersize=12,
                    transform=ax.transAxes, zorder=2)

            xytext = (0.05, y) if side == "left" else (0.95, y)
            ha = "right" if side == "left" else "left"
            color = '#6366F1' if side == "left" else '#F59E0B'
            facecolor = '#EEF2FF' if side == "left" else '#FFF7ED'

            ax.annotate(
                f"{event['date']}\n{event['event']}",
                xy=(0.5, y), xytext=xytext,
                textcoords=ax.transAxes, xycoords=ax.transAxes,
                fontsize=10, ha=ha, va='center', color='#1e1b4b',
                bbox=dict(boxstyle='round,pad=0.4', facecolor=facecolor,
                          edgecolor=color, linewidth=1),
                arrowprops=dict(arrowstyle='->', color=color),
            )

        ax.set_title(title, fontsize=14, fontweight='bold', color='#1a1a2e', pad=15)
        plt.tight_layout()
        return self._fig_to_bytes(fig)

    # ── Schéma conceptuel ──────────────────────────────────────────────

    async def concept_map(self, central: str, branches: list[dict], title: str = "") -> bytes:
        fig, ax = plt.subplots(figsize=(10, 7))
        ax.axis('off')
        ax.set_xlim(0, 10)
        ax.set_ylim(0, 7)

        ax.add_patch(mpatches.FancyBboxPatch(
            (3.5, 2.8), 3, 1.4,
            boxstyle="round,pad=0.1",
            facecolor='#6366F1', edgecolor='#4338CA', linewidth=2
        ))
        ax.text(5, 3.5, central, ha='center', va='center',
                fontsize=13, fontweight='bold', color='white')

        colors = ['#10B981', '#F59E0B', '#EF4444', '#3B82F6', '#8B5CF6', '#EC4899']
        angles = np.linspace(0, 2 * np.pi, len(branches), endpoint=False)

        for i, (branch, angle) in enumerate(zip(branches, angles)):
            x = 5 + 3.2 * np.cos(angle)
            y = 3.5 + 2.2 * np.sin(angle)
            c = colors[i % len(colors)]

            ax.annotate("", xy=(x, y), xytext=(5, 3.5),
                        arrowprops=dict(arrowstyle='->', color=c, lw=1.5))

            ax.add_patch(mpatches.FancyBboxPatch(
                (x - 1.2, y - 0.4), 2.4, 0.8,
                boxstyle="round,pad=0.05",
                facecolor=c + '22', edgecolor=c, linewidth=1.5
            ))
            text = f"{branch['label']}\n{branch.get('detail', '')}"
            ax.text(x, y, text, ha='center', va='center',
                    fontsize=9, color='#1e1b4b')

        if title:
            ax.set_title(title, fontsize=14, fontweight='bold', color='#1a1a2e', pad=15)

        plt.tight_layout()
        return self._fig_to_bytes(fig)

    # ── Utilitaires ────────────────────────────────────────────────────

    def _fig_to_bytes(self, fig) -> bytes:
        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=150, bbox_inches='tight', facecolor='white')
        plt.close(fig)
        buf.seek(0)
        return buf.read()

    def extract_latex(self, text: str) -> list[str]:
        patterns = [
            r'\$\$(.+?)\$\$',
            r'\$(.+?)\$',
            r'\\\((.+?)\\\)',
            r'\\\[(.+?)\\\]',
        ]
        formulas = []
        for pattern in patterns:
            found = re.findall(pattern, text, re.DOTALL)
            formulas.extend(found)
        return list(set(formulas))

    def has_math(self, text: str) -> bool:
        return bool(self.extract_latex(text))


image_generator = ImageGenerator()