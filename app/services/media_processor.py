"""
Détecte automatiquement les formules LaTeX dans la réponse IA
et génère les images correspondantes sans dépendre des balises.
"""
import re
from app.services.image_generator import image_generator


class MediaProcessor:

    # Patterns LaTeX à détecter
    LATEX_PATTERNS = [
        r'\$\$(.+?)\$\$',           # $$formule$$
        r'\$([^\$]+?)\$',           # $formule$
        r'\\\((.+?)\\\)',           # \( formule \)
        r'\\\[(.+?)\\\]',           # \[ formule \]
        r'\[FORMULE:\s*(.+?)\]',    # [FORMULE: formule]
        r'\[GRAPHE:\s*(.+?)\]',     # [GRAPHE: ...]
        r'\[TABLEAU:\s*(.+?)\]',    # [TABLEAU: ...]
        r'\[CHRONO:\s*(.+?)\]',     # [CHRONO: ...]
        r'\[SCHEMA:\s*(.+?)\]',     # [SCHEMA: ...]
    ]

    async def process(self, text: str) -> list[dict]:
        """
        Analyse le texte et retourne une liste de blocs à envoyer.
        Chaque bloc est : {"type": "text"|"image", "content": ...}
        """
        blocks = []

        # Cherche toutes les occurrences de balises spéciales
        all_matches = []

        # Balises explicites
        explicit_patterns = {
            "formule": r'\[FORMULE:\s*(.+?)\]',
            "graphe":  r'\[GRAPHE:\s*(.+?)\]',
            "tableau": r'\[TABLEAU:\s*(.+?)\]',
            "chrono":  r'\[CHRONO:\s*(.+?)\]',
            "schema":  r'\[SCHEMA:\s*(.+?)\]',
        }

        for tag, pattern in explicit_patterns.items():
            for m in re.finditer(pattern, text, re.DOTALL):
                all_matches.append((m.start(), m.end(), tag, m.group(1).strip(), m.group(0)))

        # Formules LaTeX inline et block
        latex_patterns = [
            r'\$\$(.+?)\$\$',
            r'\\\[(.+?)\\\]',
        ]
        for pattern in latex_patterns:
            for m in re.finditer(pattern, text, re.DOTALL):
                formula = m.group(1).strip()
                if len(formula) > 3:
                    all_matches.append((m.start(), m.end(), "formule", formula, m.group(0)))

        # Si aucune balise trouvée — cherche les formules inline simples
        # et collecte toutes ensemble en une seule image
        inline_patterns = [
            r'\$([^\$\n]{2,50}?)\$',
            r'\\\((.+?)\\\)',
        ]
        inline_formulas = []
        inline_positions = []
        for pattern in inline_patterns:
            for m in re.finditer(pattern, text, re.DOTALL):
                formula = m.group(1).strip()
                if len(formula) > 1:
                    inline_formulas.append(formula)
                    inline_positions.append((m.start(), m.end(), m.group(0)))

        # Pas de balises spéciales mais des formules inline
        if not all_matches and inline_formulas:
            # Nettoie le texte
            clean_text = text
            for _, _, original in inline_positions:
                clean_text = clean_text.replace(original, "")
            clean_text = self._clean_text(clean_text)

            if clean_text:
                blocks.append({"type": "text", "content": clean_text})

            # Génère une seule image avec toutes les formules
            unique_formulas = list(dict.fromkeys(inline_formulas))[:6]
            try:
                img = await image_generator.formula_to_image(
                    unique_formulas,
                    title="Formules"
                )
                blocks.append({"type": "image", "content": img})
            except Exception as e:
                print(f"Erreur génération formules: {e}")

            return blocks

        # Pas de formules du tout — texte simple nettoyé
        if not all_matches:
            return [{"type": "text", "content": self._clean_text(text)}]

        # Trie par position
        all_matches.sort(key=lambda x: x[0])
        # Déduplique les overlaps
        deduplicated = []
        last_end = 0
        for match in all_matches:
            if match[0] >= last_end:
                deduplicated.append(match)
                last_end = match[1]

        last_end = 0
        for start, end, tag, content, original in deduplicated:
            # Texte avant la balise
            if start > last_end:
                txt = self._clean_text(text[last_end:start])
                if txt:
                    blocks.append({"type": "text", "content": txt})

            # Génère l'image
            img_bytes = await self._generate(tag, content)
            if img_bytes:
                blocks.append({"type": "image", "content": img_bytes})
            else:
                # Fallback texte si image échoue
                blocks.append({"type": "text", "content": content})

            last_end = end

        # Texte après la dernière balise
        if last_end < len(text):
            txt = self._clean_text(text[last_end:])
            if txt:
                blocks.append({"type": "text", "content": txt})

        return blocks

    def _clean_text(self, text: str) -> str:
        """Nettoie le texte pour WhatsApp."""
        # Headers → gras
        text = re.sub(r'^#{1,6}\s+(.+)$', r'*\1*', text, flags=re.MULTILINE)
        # **gras** → *gras*
        text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)
        # Supprime LaTeX inline résiduel
        text = re.sub(r'\\\(\s*(.+?)\s*\\\)', r'\1', text, flags=re.DOTALL)
        text = re.sub(r'\\\[\s*(.+?)\s*\\\]', r'\1', text, flags=re.DOTALL)
        text = re.sub(r'\$\$(.+?)\$\$', r'\1', text, flags=re.DOTALL)
        text = re.sub(r'\$([^\$]+?)\$', r'\1', text)
        # Séparateurs
        text = re.sub(r'^---+$', '', text, flags=re.MULTILINE)
        # Lignes vides multiples
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()

    async def _generate(self, tag: str, content: str) -> bytes | None:
        try:
            if tag == "formule":
                formulas = [f.strip() for f in content.split("|") if f.strip()]
                return await image_generator.formula_to_image(formulas)

            elif tag == "graphe":
                parts = self._parse_params(content)
                expr = parts.get("expr", content.split(",")[0].strip())
                title = parts.get("titre", "Graphe")
                exprs = [e.strip() for e in expr.split("|")]
                return await image_generator.plot_function(exprs, title=title)

            elif tag == "tableau":
                parts = self._parse_params(content)
                headers = parts.get("headers", "").split("|")
                rows_raw = parts.get("rows", "")
                rows = [r.split("|") for r in rows_raw.split(";") if r.strip()]
                title = parts.get("titre", "")
                return await image_generator.data_table(headers, rows, title)

            elif tag == "chrono":
                parts = self._parse_params(content)
                events = []
                for item in content.split(";"):
                    item = item.strip()
                    if "=" in item and "titre" not in item.lower():
                        date, event = item.split("=", 1)
                        events.append({"date": date.strip(), "event": event.strip()})
                title = parts.get("titre", "Chronologie")
                if events:
                    return await image_generator.timeline(events, title)

            elif tag == "schema":
                parts = self._parse_params(content)
                central = parts.get("central", "Concept")
                branches_raw = parts.get("branches", "")
                branches = []
                for b in branches_raw.split("|"):
                    if ":" in b:
                        label, detail = b.split(":", 1)
                        branches.append({"label": label.strip(), "detail": detail.strip()})
                    else:
                        branches.append({"label": b.strip(), "detail": ""})
                if branches:
                    return await image_generator.concept_map(central, branches)

        except Exception as e:
            print(f"Erreur génération image {tag}: {e}")
        return None

    def _parse_params(self, content: str) -> dict:
        params = {}
        for part in content.split(";"):
            part = part.strip()
            if "=" in part:
                key, val = part.split("=", 1)
                params[key.strip().lower()] = val.strip()
        return params


media_processor = MediaProcessor()