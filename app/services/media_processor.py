"""
MediaProcessor simplifié — retourne toujours du texte brut.
La génération d'images (formules LaTeX, graphes) a été retirée.
"""
import re


class MediaProcessor:

    async def process(self, text: str) -> list[dict]:
        return [{"type": "text", "content": self._clean_text(text)}]

    def _clean_text(self, text: str) -> str:
        text = re.sub(r'^#{1,6}\s+(.+)$', r'*\1*', text, flags=re.MULTILINE)
        text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)
        text = re.sub(r'\\\(\s*(.+?)\s*\\\)', r'\1', text, flags=re.DOTALL)
        text = re.sub(r'\\\[\s*(.+?)\s*\\\]', r'\1', text, flags=re.DOTALL)
        text = re.sub(r'\$\$(.+?)\$\$', r'\1', text, flags=re.DOTALL)
        text = re.sub(r'\$([^\$]+?)\$', r'\1', text)
        text = re.sub(r'^---+$', '', text, flags=re.MULTILINE)
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()


media_processor = MediaProcessor()
