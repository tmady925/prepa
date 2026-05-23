"""
Traitement des documents pour le RAG.
Supporte PDF (natif + scanné), Word, Images.
"""
import io
import re
import platform
import pytesseract
import fitz  # pymupdf
import cv2
import numpy as np
from PIL import Image
from docx import Document as DocxDocument
from langchain_text_splitters import RecursiveCharacterTextSplitter

# Chemin Tesseract selon l'OS
if platform.system() == "Windows":
    pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
# Sur Linux (Render/prod), tesseract est dans le PATH automatiquement

CHUNK_SIZE = 400
CHUNK_OVERLAP = 50

# Tesseract disponible ?
TESSERACT_AVAILABLE = True
try:
    pytesseract.get_tesseract_version()
except Exception:
    TESSERACT_AVAILABLE = False
    print("Tesseract non disponible — OCR désactivé")


class DocumentProcessor:

    def __init__(self):
        self.splitter = RecursiveCharacterTextSplitter(
            chunk_size=CHUNK_SIZE,
            chunk_overlap=CHUNK_OVERLAP,
            separators=["\n\n", "\n", ".", "!", "?", " "],
        )

    async def process(self, file_bytes: bytes, filename: str) -> dict:
        """
        Traite un document et retourne le texte extrait + les chunks.
        Retourne : {
            "text": str,
            "chunks": list[str],
            "page_count": int,
            "has_ocr": bool,
            "error": str | None
        }
        """
        ext = filename.lower().split(".")[-1]

        try:
            if ext == "pdf":
                return await self._process_pdf(file_bytes)
            elif ext in ("docx", "doc"):
                return await self._process_docx(file_bytes)
            elif ext in ("png", "jpg", "jpeg", "tiff", "bmp"):
                return await self._process_image(file_bytes)
            else:
                return {"error": f"Format non supporté : {ext}"}
        except Exception as e:
            return {"error": str(e)}

    async def _process_pdf(self, file_bytes: bytes) -> dict:
        """Traite un PDF — détecte automatiquement si natif ou scanné."""
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        pages_text = []
        has_ocr = False

        for page_num in range(len(doc)):
            page = doc[page_num]

            # Essaie d'extraire le texte natif
            text = page.get_text("text").strip()

            # Si peu de texte → page scannée → OCR si disponible
            if len(text) < 50:
                if TESSERACT_AVAILABLE:
                    has_ocr = True
                    text = self._ocr_pdf_page(page)
                else:
                    print(f"Page {page_num} scannée mais Tesseract non disponible — ignorée")

            if text:
                pages_text.append(text)

        doc.close()

        full_text = "\n\n".join(pages_text)
        full_text = self._clean_text(full_text)
        chunks = self.splitter.split_text(full_text)

        return {
            "text": full_text,
            "chunks": chunks,
            "page_count": len(pages_text),
            "has_ocr": has_ocr,
            "error": None,
        }

    async def _process_docx(self, file_bytes: bytes) -> dict:
        """Traite un fichier Word."""
        doc = DocxDocument(io.BytesIO(file_bytes))
        paragraphs = []

        for para in doc.paragraphs:
            text = para.text.strip()
            if text:
                paragraphs.append(text)

        # Traite aussi les tableaux
        for table in doc.tables:
            for row in table.rows:
                row_text = " | ".join(
                    cell.text.strip() for cell in row.cells if cell.text.strip()
                )
                if row_text:
                    paragraphs.append(row_text)

        full_text = "\n\n".join(paragraphs)
        full_text = self._clean_text(full_text)
        chunks = self.splitter.split_text(full_text)

        return {
            "text": full_text,
            "chunks": chunks,
            "page_count": len(paragraphs) // 20 + 1,
            "has_ocr": False,
            "error": None,
        }

    async def _process_image(self, file_bytes: bytes) -> dict:
        """Traite une image avec OCR."""
        if not TESSERACT_AVAILABLE:
            return {
                "text": "",
                "chunks": [],
                "page_count": 1,
                "has_ocr": False,
                "error": "Tesseract non disponible sur ce serveur",
            }

        image_array = np.frombuffer(file_bytes, np.uint8)
        img = cv2.imdecode(image_array, cv2.IMREAD_COLOR)
        text = self._ocr_image(img)
        text = self._clean_text(text)
        chunks = self.splitter.split_text(text)

        return {
            "text": text,
            "chunks": chunks,
            "page_count": 1,
            "has_ocr": True,
            "error": None,
        }

    def _ocr_pdf_page(self, page) -> str:
        """OCR sur une page PDF scannée."""
        mat = fitz.Matrix(2.0, 2.0)
        pix = page.get_pixmap(matrix=mat)
        img_bytes = pix.tobytes("png")
        image_array = np.frombuffer(img_bytes, np.uint8)
        img = cv2.imdecode(image_array, cv2.IMREAD_COLOR)
        return self._ocr_image(img)

    def _ocr_image(self, img: np.ndarray) -> str:
        """OCR avec prétraitement pour améliorer les scans flous."""
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        denoised = cv2.fastNlMeansDenoising(gray, h=10)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(denoised)
        binary = cv2.adaptiveThreshold(
            enhanced, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY, 11, 2
        )
        pil_img = Image.fromarray(binary)
        text = pytesseract.image_to_string(
            pil_img,
            lang="fra",
            config="--psm 6 --oem 3"
        )
        return text.strip()

    def _clean_text(self, text: str) -> str:
        """Nettoie le texte extrait."""
        text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
        text = re.sub(r' +', ' ', text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()


document_processor = DocumentProcessor()