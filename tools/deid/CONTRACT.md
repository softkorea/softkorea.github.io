# deid module contracts (internal, for implementers)

Package root: `tools/deid/src/deid`. Python >= 3.10 (must also run on macOS M1 with python 3.10-3.13).
Run tests with: `cd tools/deid && /tmp/claude-0/venv/bin/python -m pytest -q` (install with `pip install -e .`).
Shared types live in `deid/types.py` (Span, Replacement, AnonResult, Report) and entity names in
`deid/entities.py`. Do NOT change those two files' public names; adding helpers is fine.

Design principles
- 100% local. No network calls except the optional Ollama backend, which must refuse non-loopback hosts
  unless `allow_remote=True` is passed explicitly.
- Fail closed: if a handler cannot process part of a file (unknown encoding, OCR failure), it must raise or
  emit a warning AND must not write an output that silently contains unprocessed text.
- Reports contain counts only, never raw PII values.
- Korean-first, English supported. Pure-python core; heavy deps (transformers, gliner, ocrmac) are optional
  and imported lazily inside functions.

## config.py
```python
@dataclass
class CustomPattern:
    entity: str            # e.g. "MRN" or any custom name like "STAFF_ID"
    regex: str
    score: float = 0.9
    context: list[str] = field(default_factory=list)  # if non-empty, require one of these words within 20 chars before the match

@dataclass
class Config:
    actions: dict[str, str]          # entity -> action; starts from entities.DEFAULT_ACTIONS
    labels: dict[str, str]           # entity -> Korean label; starts from entities.LABELS_KO
    names: list[str]                 # always-PERSON dictionary (exact strings, e.g. staff/patient names)
    allowlist: list[str]             # strings never treated as PII (exact match on span text)
    custom_patterns: list[CustomPattern]
    min_score: float = 0.5           # spans below this are dropped
    enabled: set[str] | None = None  # None = all entities enabled
    ner: dict = {}                   # {"backend": "hf"|"gliner"|None, "model": "...", "device": "cpu"|"mps", ...}
    ocr: dict = {}                   # {"engine": "auto"|"tesseract"|"vision", "lang": "kor+eng", "min_conf": 40}
    llm: dict = {}                   # {"model": "gemma3:4b", "host": "http://127.0.0.1:11434"}

def load_config(path: str | os.PathLike | None = None) -> Config
    # path None -> looks for $DEID_CONFIG, then ~/.config/deid/config.toml; missing file -> defaults.
    # TOML layout documented in README: [actions], [labels], names=[...], allowlist=[...], [[patterns]], [ner], [ocr], [llm]
def default_config() -> Config
```

## recognizers/base.py
```python
class Recognizer(Protocol):
    name: str
    entities: tuple[str, ...]
    def find(self, text: str, cfg: Config) -> list[Span]: ...
```
Helper available to all recognizers: `context_before(text, start, window=20) -> str` and
`context_around(text, start, end, window=20) -> str` (lowercased).

## recognizers/patterns.py  (regex + validators; Korean IDs)
`def pattern_recognizers(cfg: Config) -> list[Recognizer]` returns recognizers for:
RRN, FRN (주민/외국인번호: YYMMDD + [-space]? + gender digit 1-8 + 6 digits; date part must be a valid
calendar date; checksum raises score but is NOT required because numbers issued since 2020-10 have no
valid checksum; digit 1-4 -> RRN, 5-8 -> FRN), PHONE (01X mobile with/without hyphens/dots/spaces,
+82 10 ..., 02/031-064 landlines, 070, 050X, 15XX/16XX/18XX 8-digit business numbers), EMAIL, CARD (13-19
digits with separators, Luhn required), ACCOUNT (only with context words: 계좌, 은행, 입금, 예금주, 국민, 신한,
우리, 하나, 농협, 기업, 카카오뱅크, 토스 ...), PASSPORT (M/S/R/G/D + 8 digits or new format like M123A4567,
with context or uppercase-letter rule), DRIVER_LICENSE (NN-NN-NNNNNN-NN, region-name or 2-digit prefix),
BRN (NNN-NN-NNNNN with checksum), HEALTH_INSURANCE (context: 건강보험, 보험증, 증번호 + 10-11 digits), MRN
(context: 등록번호, 차트번호, 환자번호, 병록번호, MRN, Chart No, ID + 4-10 digit/alnum id), IP (v4 valid
octets; v6), PLATE (12가3456 / 123가4567 / 서울12가3456), BIRTHDATE (date expression preceded within ~15
chars by 생년월일, 생일, 출생, DOB, birth, or patterns like "1985.03.02생", "85년생"), DATE (YYYY-MM-DD,
YYYY.MM.DD, YYYY/MM/DD, YYYY년 M월 D일, YY.MM.DD only when valid), URL, plus Config.custom_patterns.
Each recognizer returns Spans whose `text == text[start:end]` and should not include surrounding
punctuation or Korean particles (조사).

## recognizers/address.py
`class AddressRecognizer` (entity ADDRESS). Detect Korean road-name and lot-number addresses:
시/도 (서울특별시, 서울시, 서울, 경기도, 경기, 부산광역시, ... 제주특별자치도, 세종특별자치시, 강원특별자치도, 전북특별자치도) +
시/군/구 + (읍/면/동/리 | 로/길 + 번호) + optional building number, 아파트/빌라 name, 동/호. Also partial
addresses that start at 시/군/구 level with a road name and number. Must expose
`def generalize_address(text: str) -> str` returning the 시/도 + 시/군/구 prefix (e.g. "서울특별시 강남구") or
"" if none can be determined.

## recognizers/names.py
`class KoreanNameRecognizer` and `class EnglishNameRecognizer` (entity PERSON).
Korean: surname list (top ~120 Korean surnames incl. 2-syllable 남궁, 황보, 제갈, 선우, 독고, 사공, 서문) +
1-2 syllable given name. Because bare surname+syllables collides with common words (이상, 정상, 조사, 
문제, 신경, 장기, 고혈압 ...), require ONE of:
  (a) honorific/role directly after: 님, 씨, 환자, 환자분, 선생님, 선생, 원장, 원장님, 교수, 교수님, 간호사, 
      보호자, 군, 양, 할머니, 할아버지, 어머니, 아버지, 어르신, 과장, 팀장, 실장, 대표, 기사
  (b) label directly before (optionally with ':' or ' '): 이름, 성명, 환자명, 환자, 보호자, 담당의, 주치의,
      의사, 작성자, 신청인, 수신, 참조, 담당, 예금주, 성함
  (c) exact match in cfg.names (dictionary), any context.
Span covers only the name (no honorific, no particle). Names followed by particles (가, 이, 는, 은, 를,
을, 의, 에게, 께, 께서, 와, 과, 도, 만, 랑, 이랑, 한테) must be trimmed correctly, e.g. "김민준이" -> "김민준"
only when the trailing 이 is a particle (3-syllable name + 이) — be conservative.
English: titles (Mr, Mrs, Ms, Miss, Dr, Prof) + Capitalized Name(s); labels (Name:, Patient:); cfg.names.
Allowlist and a built-in stopword list prevent common medical words from matching.

## engine.py
```python
class Detector:
    def __init__(self, cfg: Config | None = None, recognizers: list[Recognizer] | None = None,
                 extra_recognizers: list[Recognizer] | None = None)
        # recognizers None -> pattern_recognizers + AddressRecognizer + name recognizers + NER backend if cfg.ner.backend
    def detect(self, text: str) -> list[Span]
        # run all recognizers, drop disabled entities, allowlist, score < min_score, then resolve overlaps,
        # then propagation within this text (see below). Returned spans are sorted, non-overlapping.
    def detect_document(self, chunks: list[str]) -> list[list[Span]]
        # same as detect on each chunk, but propagation uses values found in ANY chunk (e.g. a name found
        # in an Excel "이름" column is masked in free-text cells too). Returns one list per chunk.
    def add_known(self, entity: str, value: str) -> None
        # register a value (e.g. from an Excel name column) that must be found everywhere via propagation.
```
Overlap resolution: higher score wins; tie -> longer span; tie -> earlier start.
Propagation: every detected PERSON/EMAIL/PHONE/RRN/MRN value (and add_known values) is searched for as
an exact substring in all chunks; PERSON matches must be at a Korean word boundary (start of text,
whitespace, punctuation before; end of text, whitespace, punctuation, or a particle/honorific after) and
for English at \b boundaries. Propagated spans have source="propagate" and score 0.85.

## anonymizer.py
```python
class Anonymizer:
    def __init__(self, cfg: Config | None = None, vault: Vault | None = None)
    def replacement_for(self, span: Span) -> tuple[str, str]   # (replacement_text, action)
    def anonymize(self, text: str, spans: list[Span]) -> AnonResult
```
Actions (see entities.DEFAULT_ACTIONS):
- pseudo: consistent per (entity, normalized value) token "[{label}{n}]" e.g. [이름1], [이름2], [등록번호1];
  same value -> same token across the whole run (and across files when the same Vault is reused).
  Normalization: strip spaces/hyphens for numeric IDs, casefold emails.
- mask: PERSON 김민준 -> 김*준, 이수 -> 이*; PHONE 010-1234-5678 -> 010-****-5678 (keep separators/shape);
  RRN 900101-1234567 -> 900101-1******; CARD keep last 4; EMAIL a***@domain; other -> keep first and last char.
- redact: "[{label}]" e.g. [주민번호]
- generalize: ADDRESS -> generalize_address(text) + " [주소]" or "[주소]"; BIRTHDATE -> "YYYY년생" (or
  "[생년월일]" if year unknown); DATE -> "YYYY년 M월"; other entities fall back to redact.
- keep: unchanged text, still recorded as a Replacement with action "keep".

## vault.py
```python
class Vault:
    def __init__(self) ; tokens: dict[str, str] (token -> original); key map (entity, norm) -> token
    def token_for(self, entity: str, original: str, label: str) -> str
    def restore(self, text: str) -> str        # replace every known token with its original value
    def save(self, path, passphrase: str) -> None   # scrypt-derived key + Fernet (cryptography); file mode 0600
    @classmethod
    def load(cls, path, passphrase: str) -> "Vault"  # wrong passphrase -> raises VaultError
```

## backends/
- `ner_hf.py`: `class HFNerRecognizer(model: str, label_map: dict[str,str] | None, device="cpu", min_score=0.6)`
  using `transformers.pipeline("token-classification", aggregation_strategy="simple")`, lazy import,
  maps model labels (PER, PS, NAME, ADDRESS, PHONE ...) to deid entities. Long text chunked with offsets.
- `gliner_backend.py`: `class GlinerRecognizer(model="fastino/gliner2-pii" or urchade/gliner_multi_pii-v1, labels=...)`.
- `ollama.py`: `def llm_review(text: str, model: str, host="http://127.0.0.1:11434", allow_remote=False,
  timeout=120) -> list[dict]` returns [{"text": "...", "entity": "PERSON", "reason": "..."}] of PII the
  LLM thinks REMAINS in already-deidentified text; only items that literally occur in `text` are kept.
  Uses urllib only; `format: "json"`; refuses non-loopback hosts unless allow_remote.

## formats/  (each handler: `def process(src: Path, dst: Path, detector: Detector, anonymizer: Anonymizer, cfg: Config) -> Report`)
- text.py: .txt .md .log .json .xml .html (encoding detection: utf-8, utf-8-sig, cp949/euc-kr fallback;
  output in the same encoding). Also `def deid_text(text, detector, anonymizer) -> tuple[str, Report]`.
- tabular.py: .xlsx/.xlsm (openpyxl, keep formatting, all sheets, header-row heuristics: columns whose
  header matches 이름/성명/환자명/보호자 -> PERSON for every cell and add_known; 주민번호/주민등록번호 -> RRN;
  생년월일/생일 -> BIRTHDATE; 전화/연락처/휴대폰 -> PHONE; 주소 -> ADDRESS; 이메일 -> EMAIL;
  등록번호/차트번호 -> MRN; other text cells -> detector), formulas untouched but warn, workbook
  properties (creator, lastModifiedBy) cleared, comments processed; .csv/.tsv same heuristics.
- pdf.py: text PDFs via PyMuPDF words -> spans -> add_redact_annot + apply_redactions (true removal),
  scanned pages (no text layer) -> render -> image redaction -> rebuild page; metadata cleared.
- docx.py: python-docx paragraphs (body, tables, headers/footers), replacement across runs keeps the first
  run's formatting; core properties cleared.
- hwpx.py: zip, process text nodes in Contents/section*.xml (<hp:t>), keep everything else byte-identical.
- image.py: OCR (Apple Vision via ocrmac on macOS if installed, else pytesseract kor+eng) -> words/lines
  with boxes -> detector on line text -> fill black boxes (or blur) over the matched character ranges ->
  save. `def redact_image(img: PIL.Image, detector, cfg) -> tuple[PIL.Image, Report]`.

## cli.py, mac.py, watch.py, web.py
`deid file PATH... [-o OUTDIR]`, `deid text` (stdin->stdout), `deid clip` (pbpaste->pbcopy), `deid shot`
(screencapture -i -> redact -> clipboard + ~/Deid/out), `deid restore` (vault restore stdin/clipboard),
`deid watch IN OUT`, `deid serve` (127.0.0.1 only web UI), `deid check FILE --llm MODEL`, `deid doctor`
(environment check: tesseract/kor, ocrmac, ollama reachable).
