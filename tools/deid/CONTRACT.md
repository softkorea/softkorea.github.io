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

## Amendments after core build (authoritative where they differ from the text above)
- Custom regex patterns: the span is the named group `pii` if present, otherwise the whole match
  (plain capture groups never narrow the span).
- New entities: MEDICAL_LICENSE (보건의료인 면허번호/전문의 자격번호, context required, redact) and OTHER
  (LLM-reported identifier of unknown type, redact, label 기타).
- English PERSON propagation uses ASCII boundaries (no ASCII letter/digit touching the match), so Korean
  particles may follow an English name ("John이").
- Email mask keeps the local-part length: minjun@example.com -> m*****@example.com.
- Vault: Hangul PERSON values ignore internal spaces for token identity; restore() returns first spelling.
- BIRTHDATE generalization never yields a future year; DATE uses pivot 30 for 2-digit years.
- `deid.recognizers.patterns.parse_date(text) -> (year, month, day)` is public.
- `api.deidentify(text, cfg=None, vault=None, *, detector=None) -> (str, Report)`; `deid` package lazily
  exports deidentify, Detector, Anonymizer, Vault, VaultError, Config, default_config, load_config, Span, Report.
- NER backends: cfg.ner["backend"] in {"hf", "gliner", "ollama"/"llm"}; GLiNER default model is
  urchade/gliner_multi_pii-v1 (`gliner` package). Extra [ner] keys: label_map, pii_model, max_chars, overlap,
  labels, threshold, host, allow_remote, timeout.

### Amendments after the red-team round (2026-10; authoritative where they differ)

Engine and recognizers
- Input folding: `Detector` runs recognizers on a folded copy of the text - non-NFC text
  (macOS NFD Hangul) is composed to NFC with a cluster offset map; full-width ASCII and Unicode
  horizontal spaces (NBSP ...) map 1:1; dash look-alikes between digits (incl. `ㅡ`) become `-`.
  Returned spans always slice the ORIGINAL text (`span.text == text[start:end]`).
- `Detector.detect(text, known=None)` / `detect_document(chunks, known=None)`: `known` adds
  per-call values (`{entity: [values]}` or `[(entity, value), ...]`) that are propagated like
  `add_known` values but not remembered. `Detector.clear_known()` forgets every `add_known`
  value (they otherwise live as long as the detector: a name typed in one file of a run is
  masked in the next file too).
- Propagation uses hash-set lookups at candidate positions (no giant alternation regex; 60k
  known values cost ~0.1 s per detect). It may replace a `keep` span (DATE) with a known value
  of another entity (BIRTHDATE). Given-name forms after a full name (`민준이가`, `하준이 엄마`,
  `서연아`, `혜진쌤`) get their own token; skipped when > 300 bulk known PERSON values exist.
- New rule families: RRN halves and separators (1800s RRNs need a valid checksum; RRNs whose
  birth date is in the future are rejected), phone separators, MRN label variants, birth-date
  formats, spoken numbers, foreign passports, account holders, obfuscated e-mails, US/UK phones,
  apartment units, romanized/English addresses, more name labels/honorifics, patient-list
  layouts, English signature/relation names, and an OTHER reference-ID recognizer
  (증권번호, 사고접수번호, 청구번호, 사번 ...; action redact, label 기타).
- `진료의, 처방의, 판독의, 집도의, 의뢰의` are weak labels (after a single space only a 3+
  syllable name counts). EMR header words (`상병명`, `진단명`, `진료일` ...) are name stopwords.
- `api.deidentify()` runs `formats.text.deid_text()` (same pipeline as `deid text` / `deid clip`):
  tables pasted as text are typed by column header (see below).

Vault
- `normalize_value` applies NFKC first (full-width / NFD spellings share a token).
- `Vault.items() -> [(entity, original, token)]` in issue order.
- `Vault.restore(text, *, links=True)`; `links=False` leaves tokens inside URLs and e-mail links
  (`vault.link_spans(text)`: http(s)/ftp/ws/file/mailto/sms links, `www.`, `//host` after
  `(`/`=`/quotes) unrestored, so an LLM-chosen link cannot carry real values off the computer.
  `Vault.tokens_in_links(text)` counts them. `deid restore`, `deid text --restore` and the web
  restore use `links=False` and report the count (`/api/restore` returns `in_links`).

Config (`config.toml`)
- Top level: `names`, `allowlist`, `min_score`, `enabled`, `disabled`; tables `[actions]`,
  `[labels]`, `[columns]` ("header" = TYPE | "skip" | "text"), `[[patterns]]`, `[ocr]`, `[ner]`, `[llm]`.
  Without tomllib/tomli (Python 3.10 without the dependency) a built-in TOML-subset reader
  (`config.parse_toml`) is used; `config_from_dict(data, base=None)` applies a parsed dict.
- `[ocr]` keys: `engine` (auto|livetext|vision|tesseract), `lang`, `psm`, `adaptive`, `upscale`,
  `invert`, `min_conf`, `timeout`, `tesseract_cmd`, `languages`, `recognition_level`,
  `space_gap`, `style` (box|blur|pixelate), `verify`, `orientation` (auto|true|false),
  `barcodes` (cover|warn|off), `bare_names`, `redact_faces` (warning only), `pdf_images`
  (auto|true|false), `pdf_dpi` (300), `docx_images` / `hwpx_images` ("auto" default | true =
  all, fail closed without OCR | false), `max_image_pixels` (100 M), `max_frames` (300),
  `max_total_pixels` (1.5 G).
- `[llm]`: `model` (default gemma3:4b), `host`, `allow_remote`, `timeout`.

Formats
- Pasted tables (`formats.text.table_spans`): tab-separated rows always; comma-separated rows
  only under a recognised header row AND each typed cell must look like its kind (PERSON:
  `name_shaped`; others: `text.value_shaped`, see the v0.2.0 amendments). `Batch.add(text, setter, entity=None, *,
  typed=spans)` takes such cell spans. `tabular.normalize_header` strips only 1-2 trailing
  digits ("연락처1"), so a cell holding a value ("주민번호 900101-...") is never a header.
- Zip budget for every OOXML/HWPX read (`formats.text.zip_size_problem`, `ZIP_TOO_BIG_ERROR`):
  512 MiB total, 256 MiB per entry, 10k entries, 200:1 ratio above 32 MiB; also `deid check`.
- `docx.parse_xml` refuses XML parts that declare entities in an internal DTD (`DTD_ERROR`;
  entities are never expanded, so their text would bypass detection). ActiveX `.bin` parts are
  emptied like embeddings. Embedded images are size-checked (`image.check_pixels`) before
  decoding; too large -> counted as unchecked.
- Images: `image.check_pixels(size, cfg, frames=1, total=None)` raises `ImageTooLargeError`
  before decoding. Apple Vision lines: `ocr.line_from_box(text, box, conf, engine, gray=None)`
  places words at their ink columns (`ocr.ink_word_extents`, `OcrLine.aligned`); estimated
  boxes get extra padding of `image.DRIFT_RATIO` x the distance to the nearer anchor (word or
  line end). Empty Apple OCR results must be confirmed by another engine (else `OcrError`).
- `formats.deid_filename` NFC-normalises the name and also pseudonymises bare name-shaped
  words (`guess_name_spans`). `formats.iter_files` skips symbolic links (files and folders).

CLI / web / session / mac
- `deid serve`: uploads are processed in `$TMPDIR/deid-web-<pid>-*`; SIGHUP/SIGTERM stop the
  server like Ctrl-C; `web.sweep_work_dirs()` deletes folders of dead servers at start and the
  server's own at exit. `_Handler.timeout` (15 s) drops idle connections.
- `mac.keychain_set` sends the secret to `security -i` on stdin (never in argv);
  `mac.keychain_get` returns None only for "not found" (exit 44) and raises `MacError` otherwise.
- `session.purge_expired_session()` (every command, watch pass, and every 60 s in `deid serve`),
  `session.make_private_dir()` / `prepare_work_dir()` (0700 work folders).
- `cli.install_hint(*packages)` names `pipx inject <venv>` or `<python> -m pip install`.
- User documentation: `README.md` (Korean, for the clinician) and `macos/README.md`.

### Amendments after the completeness review (2026-10, v0.2.0; authoritative where they differ)

Engine and recognizers
- Containers: `engine.resolve_overlaps(spans, is_keep=None)`. A span whose action is `keep`
  (URL, DATE, or any entity configured `keep`) that FULLY contains a span of another action is
  dropped, so the inner span wins and the rest stays as written; partial overlaps are still
  decided by score. `Detector` passes `is_keep`; propagation of known values also opens kept
  containers (a known name in a URL path is masked). With `URL` not `keep`, URLs are no container.
- `patterns.UrlParamRecognizer` (name `url_param`): values after `?`/`&`/`;`/`#` for personal
  keys (name, patient_name, rrn, jumin, tel, phone, hp, pid, mrn, email, birth, dob ...), only
  when the value has a plausible shape (`?name=report.pdf`, `?contact=us` stay). An IPv4 right
  after `//` is a host and follows the IP action.
- Birth dates without a 생년월일 label: date + 출생/생일/태어/탄생; `생 1979.4.5`, `b. 1979.4.5`;
  a date in a parenthetical with a sex/age marker (`(79.04.05, F)`, `(1979.04.05/F)`), the age
  consistent with the date; future dates, a date label before the parenthesis (검사일 ...),
  event words, F/U and 예정 block it. `patterns.birthdate_after_name(text, start, end)` is public;
  the Detector turns DATE into BIRTHDATE for `PERSON (date ...)`. names: `name(date, sex/age)`
  is PERSON evidence; `#NNNN` after a bare M/F or a date is an MRN.
- Addresses: a value after 주소/주소지/현주소/거주지/자택/송부지 ... is parsed word by word and
  needs a real address feature (chief complaints such as `주소: 두통` stay); home units without
  동/호 (`행복빌라 302호`, `102-1501 행복아파트`), labelled postal codes and `(06236) <address>`.
  `generalize_address` skips a leading postal code; `_extend_left` stops at the first
  non-administrative word after a 시/구 word (`동행`, `동안` ... are not 동 names).
- 이니셜 and 이메일 are name stopwords; initials after 이니셜/initials are PERSON. A BRN whose
  checksum fails is accepted (score 0.85) right after its own 사업자(등록)번호/BRN label.

Vault
- `Vault.restore(text, *, links=True, particles=True)`: directly after a restored token the
  Korean particle is fixed to the restored value (와/과, 은/는, 이/가, 을/를, 로/으로 with the
  ㄹ rule, 랑/이랑, 아/야; a final digit uses its Sino-Korean reading). Helper
  `vault.fix_particle(value, text, pos) -> (particle, length) | None`.

Config (`deid.config`)
- Loading validates and raises ValueError with a Korean message (nearest name via difflib) for
  unknown top-level keys or tables, unknown entity names in `[actions]`/`[labels]`/`enabled`/
  `disabled`, invalid actions, bad `[columns]` values and invalid `[[patterns]]` (entity and
  regex required, regex must compile). Entity names are case-insensitive and Korean labels
  (이름, 전화번호 ...) are accepted; Korean action aliases (가명, 삭제 ...) too; custom
  `[[patterns]]` entities are valid everywhere. `[columns]` values normalise to an entity name,
  `skip` or `text`. TOML errors get Korean messages (a duplicate `[table]` hints at pasting the
  example below an existing file; the built-in 3.10 reader also rejects duplicates).
- `names`/`allowlist` found inside another table are lifted to the top level with a warning.
- `read_toml(path) -> dict`, `validate(path=None) -> (errors, warnings)`,
  `validate_report(path=None) -> list[str]`; `config_from_dict(data, base=None, *, problems=None)`
  appends soft warnings to `problems`.

Formats
- Comma tables in prose: each typed cell must fit `text.value_shaped(entity, value)` (BIRTHDATE/
  DATE need `text.date_shaped`, PHONE a phone-like digit run that is not a date or an amount,
  EMAIL/IP/PLATE their exact shapes, ID entities a single identifier, custom types short values).
  `tabular.plausible` is unchanged and still used for xlsx header detection.
  `column_entity` never takes a cell holding a date or an age for a header.
- `merge_typed` (with a strength function; Batch always passes one): a detected span that runs
  past the typed cells it overlaps keeps its outside part; a typed keep/mask span gives way to a
  stronger detected span inside it. A phone-column cell types only the number; the rest is
  detected, and a lone name-shaped word next to the number is typed PERSON (TSV, CSV, xlsx).
- Images (`image._Redactor._snap_to_ink`): every non-estimated (tesseract) box grows outward over
  the ink it cuts through until a blank run of 0.2 x line height (at most 0.6 x line height),
  then gets 0.12 x line height horizontal / 0.05 vertical padding, stopping one pixel short of
  the next word's ink. OCR read one syllable at a time keeps a spaced variant after name labels;
  a label-slot value is at most 4 syllables of whole words.
- `formats.HINTS` (DICOM, GIF, e-mail, video/audio): `unsupported_message()` says what to do for a
  single file; folder runs and the watch folder still only skip these (`is_legacy` is False).

CLI / watch / web / session
- `watch.LiveConfig(path=None, cfg=None, *, follow=True)` follows the config file (mtime, size,
  inode, ctime); `refresh() -> ConfigChange | None`; a file that does not load keeps the
  previous Config and sets `.error`; `LiveConfig.static(cfg)` never reloads. Helpers
  `config_error_text(e, path=None)`, `config_location(path=None) -> (Path, explicit)`.
  `run_watch(..., cfg=)` takes a LiveConfig, a Config (fixed) or None (follow the default file)
  and refreshes before every pass. `DeidServer` keeps `.live`, refreshes after the token check of
  each request, and adds `config_warning` to authenticated JSON / `X-Deid-Report` while the file
  is broken (page banner `#config-warning`).
- A config that does not load stops every command (Korean message, exit 2, no traceback);
  `install-watch` refuses to install with a broken config; `deid doctor` shows [XX] for errors
  and [!!] for each `validate_report` warning. `deid text` stops with exit 2 and no output when
  detection raises RuntimeError (e.g. a `[ner]` backend that is not installed).
- Version 0.2.0. `cli.build_info()` (never raises): git commit/branch read from `.git` files for a
  source checkout (never runs git), else pip's `direct_url.json` (commit and URL without
  credentials; a folder install adds `, 설치 YYYY-MM-DD HH:MM`), else the install folder.
  `cli.version_text()` backs `deid --version`; doctor, the watch log and the serve banner show it.
- Session: `session.json` holds `created` and `last_used` (each successful load/save updates
  `last_used`). Expiry = idle >= `DEID_SESSION_HOURS` (12); `last_used` more than 1 h in the
  future also expires; 0.1 files fall back to `created`. `session_idle_hours()`,
  `forget_session_items(describe=False) -> list[str]` (what `deid forget` prints; never values).
- Vault passphrase: `cli._can_prompt()` (stdin TTY, or stderr TTY with /dev/tty);
  without a terminal `NO_TTY_PASS_ERROR` names DEID_VAULT_PASS (`deid restore --notify` also
  notifies).
