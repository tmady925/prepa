"""
Détecte les balises spéciales dans la réponse IA
et génère les images correspondantes.
"""
import re
from app.services.image_generator import image_generator


class MediaProcessor:

    async def process(self, text: str) -> list[dict]:
        """
        Analyse le texte et retourne une liste de blocs à envoyer.
        Chaque bloc est : {"type": "text"|"image", "content": ...}
        """
        blocks = []
        remaining = text

        # Patterns à détecter
        patterns = {
            "formule": r'\[FORMULE:\s*(.+?)\]',
            "graphe":  r'\[GRAPHE:\s*(.+?)\]',
            "tableau": r'\[TABLEAU:\s*(.+?)\]',
            "chrono":  r'\[CHRONO:\s*(.+?)\]',
            "schema":  r'\[SCHEMA:\s*(.+?)\]',
        }

        # Trouve toutes les balises avec leur position
        all_matches = []
        for tag, pattern in patterns.items():
            for m in re.finditer(pattern, remaining, re.DOTALL):
                all_matches.append((m.start(), m.end(), tag, m.group(1).strip()))

        if not all_matches:
            return [{"type": "text", "content": text}]

        all_matches.sort(key=lambda x: x[0])

        last_end = 0
        for start, end, tag, content in all_matches:
            # Texte avant la balise
            if start > last_end:
                txt = remaining[last_end:start].strip()
                if txt:
                    blocks.append({"type": "text", "content": txt})

            # Génère l'image
            img_bytes = await self._generate(tag, content)
            if img_bytes:
                blocks.append({"type": "image", "content": img_bytes, "tag": tag})

            last_end = end

        # Texte après la dernière balise
        if last_end < len(remaining):
            txt = remaining[last_end:].strip()
            if txt:
                blocks.append({"type": "text", "content": txt})

        return blocks

    async def _generate(self, tag: str, content: str) -> bytes | None:
        try:
            if tag == "formule":
                formulas = [f.strip() for f in content.split("|")]
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
                    if "=" in item and not any(k in item for k in ["titre", "title"]):
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
        """Parse les paramètres clé=valeur séparés par ;"""
        params = {}
        for part in content.split(";"):
            part = part.strip()
            if "=" in part:
                key, val = part.split("=", 1)
                params[key.strip().lower()] = val.strip()
        return params


media_processor = MediaProcessor()