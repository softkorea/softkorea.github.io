"""Regex + validator recognizers: Korean IDs, contacts, cards, network IDs, plates, dates."""
from __future__ import annotations

import bisect
import datetime
import re
from typing import TYPE_CHECKING, Iterable

from .. import entities as E
from ..types import Span
from . import validators as V
from .base import RegexRecognizer, make_span

if TYPE_CHECKING:
    from ..config import Config
    from .base import Recognizer

_A = re.ASCII
_AI = re.ASCII | re.IGNORECASE

# Not glued to a neighbouring number: "12-<x>", "<x>-34", "1.<x>" all count as glued.
_NB = r"(?<![0-9])(?<![0-9][-.])"
_NA = r"(?![0-9])(?![-.][0-9])"
_ASCII_ALNUM = re.compile(r"[A-Za-z0-9_]")
_HAS_DIGIT = re.compile(r"[0-9]")
_HANGUL_FIRST, _HANGUL_LAST = "가", "힣"

# Units that turn "1500-1800" or "37도 2000" into a quantity rather than an identifier.
_UNIT_AFTER = re.compile(
    r"[ \t]*(?:kcal|cal|cc|ml|mg|mcg|ug|kg|g|l|mm|cm|km|m|iu|u|ms|hz|%|원|명|개|회|건|단위|"
    r"칼로리|시간|시|분|초|년|월|일|세|미터|그램|리터|번째)(?![a-z])",
    _AI,
)


def _ctx(rx: re.Pattern, text: str, start: int, end: int, before: int = 20, after: int = 0) -> bool:
    """True if `rx` occurs within `before` chars before or `after` chars after [start, end)."""
    if rx.search(text, max(0, start - before), start):
        return True
    return bool(after) and rx.search(text, end, min(len(text), end + after)) is not None


def _glued_alpha(text: str, start: int, end: int) -> bool:
    """True if an ASCII letter/digit/underscore touches the span on either side."""
    return bool((start > 0 and _ASCII_ALNUM.match(text[start - 1]))
                or (end < len(text) and _ASCII_ALNUM.match(text[end])))


def _is_hangul(ch: str) -> bool:
    return _HANGUL_FIRST <= ch <= _HANGUL_LAST


def _dedupe(spans: Iterable[Span]) -> list[Span]:
    """Drop spans overlapping a longer (then higher-scored) span; result sorted by start."""
    starts: list[int] = []
    kept: list[Span] = []
    for sp in sorted(spans, key=lambda s: (-(s.end - s.start), -s.score, s.start)):
        i = bisect.bisect_left(starts, sp.start)
        if (i > 0 and kept[i - 1].end > sp.start) or (i < len(kept) and kept[i].start < sp.end):
            continue
        starts.insert(i, sp.start)
        kept.insert(i, sp)
    return kept


class _Recognizer:
    name = ""
    entities: tuple[str, ...] = ()

    def find(self, text: str, cfg: "Config | None" = None) -> list[Span]:  # pragma: no cover
        raise NotImplementedError

    def _span(self, text: str, start: int, end: int, entity: str, score: float) -> Span:
        return make_span(text, start, end, entity, score, self.name)


# --------------------------------------------------------------------------- RRN / FRN

_MASK = "*xX●○•#＊"  # * x X ● ○ • # ＊(fullwidth)
# Hyphen / dash variants, "/", "·", ".", "_", "~" (with optional spaces) or one or two spaces.
# Dash look-alikes such as "ㅡ" or full-width "－" are folded to "-" by the engine first.
_RRN_SEP = r"(?P<sep>[ \t]?[-‐‑–—/·._~][ \t]?|[ \t]{1,2})?"
_YYMMDD = r"(?P<date>[0-9]{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12][0-9]|3[01]))"
_RRN_NB = r"(?<![A-Za-z0-9])(?<![0-9][-.])"
_RRN_RX = re.compile(
    _RRN_NB + _YYMMDD + _RRN_SEP
    + rf"(?P<tail>[0-9{_MASK}]{{7}})(?![A-Za-z0-9{_MASK}])(?![-.][0-9])",
    _A,
)
# "900101-1" (birth date + gender digit only) is too ambiguous without context.
_RRN_SHORT_RX = re.compile(
    _RRN_NB + _YYMMDD + rf"[ \t]?-[ \t]?(?P<g>[1-8])(?![A-Za-z0-9{_MASK}])", _A
)
_CTX_RRN = re.compile(r"주민|등록\s?번호|외국인|생년|\brrn\b|\bfrn\b|\bssn\b", _AI)
# The number written in halves: "주민번호 뒷자리 2048117", "뒤 7자리 1538214" (front half: DateRecognizer).
_RRN_BACK_RX = re.compile(
    r"(?:뒷[ \t]?(?:자리|번호|부분|7[ \t]?자리)|뒤[ \t]?(?:7[ \t]?)?(?:자리|번호))"
    r"[ \t]*(?:[:：=]|은|는|이|가|숫자는?)?(?:[ \t]+[가-힣]{1,4})?[ \t]*(?P<v>[1-8][0-9]{6})(?![0-9])(?![-.][0-9])",
    _A,
)
_CTX_RRN_HALF = re.compile(r"주민|외국인[ \t]?등록|등록[ \t]?번호|\brrn\b|\bssn\b|앞[ \t]?(?:자리|6[ \t]?자리)", _AI)


def _born_not_future(date: str, gender_digit: str) -> bool:
    """The birth date implied by YYMMDD + gender digit is not after today."""
    century = V.rrn_century(gender_digit)
    if century is None:
        return True
    born = datetime.date(century + int(date[:2]), int(date[2:4]), int(date[4:]))
    return born <= datetime.date.today()


class RrnRecognizer(_Recognizer):
    """주민등록번호 / 외국인등록번호, including masked forms that still leak the birth date."""

    name = "kr_rrn"
    entities = (E.RRN, E.FRN)

    def find(self, text: str, cfg: "Config | None" = None) -> list[Span]:
        if not _HAS_DIGIT.search(text):  # every shape needs an ASCII digit
            return []
        out: list[Span] = []
        for m in _RRN_RX.finditer(text):
            date, tail, sep = m.group("date"), m.group("tail"), m.group("sep")
            g = tail[0]
            entity = E.FRN if g in "5678" else E.RRN
            if tail.isdigit():
                if not V.rrn_date_ok(date, g) or not _born_not_future(date, g):
                    continue  # e.g. GS1 barcode 8801234567890 would be a birth in 2088
                d13 = date + tail
                ok = V.frn_checksum_ok(d13) if entity == E.FRN else V.rrn_checksum_ok(d13)
                if g in "90":  # 1800s births: nobody alive; only a valid checksum keeps it
                    if not ok:
                        continue
                    score = 0.9
                else:
                    score = 0.95 if ok else 0.75
            else:
                gender_known = g.isdigit()
                if not V.rrn_date_ok(date, g if gender_known else None):
                    continue
                if gender_known and not _born_not_future(date, g):
                    continue
                if not gender_known and not sep:
                    continue
                score = 0.8 if gender_known else 0.7
            out.append(self._span(text, m.start(), m.end(), entity, score))
        for m in _RRN_SHORT_RX.finditer(text):
            g = m.group("g")
            if V.rrn_date_ok(m.group("date"), g) and _ctx(_CTX_RRN, text, m.start(), m.end()):
                entity = E.FRN if g in "5678" else E.RRN
                out.append(self._span(text, m.start(), m.end(), entity, 0.7))
        for m in (_RRN_BACK_RX.finditer(text) if ("뒷" in text or "뒤" in text) else ()):
            if _ctx(_CTX_RRN_HALF, text, m.start(), m.end(), before=30, after=30):
                entity = E.FRN if m.group("v")[0] in "5678" else E.RRN
                out.append(self._span(text, m.start("v"), m.end("v"), entity, 0.85))
        return _dedupe(out)


# --------------------------------------------------------------------------- PHONE

_AREA = r"0(?:2|3[1-3]|4[1-4]|5[1-5]|6[1-4]|70|80|50[0-9])"
# Mobile numbers: each group separator on its own ("010-9912 -3304", "010 - 9912 - 3304",
# "010)5873-2216", "010~4417~2290", "010-66213098", "0106621-3098", "(010)5873-2216").
_MSEP = r"(?:[ \t]?[-.~)_][ \t]?|[ \t])"
_MOBILE_RX = re.compile(
    _NB + rf"(?:\(01[016789]\)[ \t]?|01[016789](?P<s1>{_MSEP})?)[0-9]{{3,4}}(?P<s2>{_MSEP})?[0-9]{{4}}"
    + _NA, _A)
_INTL_RX = re.compile(
    r"(?<![0-9A-Za-z+])\+82[ \t-]?(?:\(0\)[ \t-]?)?"
    r"(?:1[016789]|2|3[1-3]|4[1-4]|5[1-5]|6[1-4]|70)(?P<s>[-. ]?)[0-9]{3,4}(?P=s)[0-9]{4}" + _NA,
    _A,
)
_LAND_RX = re.compile(_NB + _AREA + r"(?P<s>[-. ]?)[0-9]{3,4}(?P=s)[0-9]{4}" + _NA, _A)
_PAREN_RX = re.compile(r"(?<![0-9])\(" + _AREA + r"\)[ \t]?[0-9]{3,4}[-. ]?[0-9]{4}" + _NA, _A)
# Landlines written "031)789-4521" (form convention) or with spaced / mixed separators.
_LAND2_RX = re.compile(
    _NB + _AREA + r"(?P<s1>\)[ \t]?|[ \t]?[-.~][ \t]?)[0-9]{3,4}(?P<s2>[ \t]?[-.~][ \t]?|[ \t])[0-9]{4}" + _NA,
    _A,
)
# North American "(503) 555-0147" / "415-555-0172" and UK mobiles "07700 900417": context only.
_NANP_RX = re.compile(
    r"(?<![0-9A-Za-z+])(?:\+?1[ .-]?)?(?:\([2-9][0-9]{2}\)[ ]?|[2-9][0-9]{2}[-. ])[2-9][0-9]{2}[-. ][0-9]{4}"
    r"(?![0-9])(?![-.][0-9])",
    _A,
)
_UK_MOBILE_RX = re.compile(r"(?<![0-9+])(?:\+44[ ]?7|07)[0-9]{3}[ ]?[0-9]{6}(?![0-9])", _A)
_BIZ_RX = re.compile(_NB + r"(?P<a>1[568][0-9]{2})(?P<s>[-. ]?)(?P<b>[0-9]{4})" + _NA, _A)
_PHONE_SHAPE = re.compile(
    r"(?:01[016789]|" + _AREA + r")[-. ]?[0-9]{3,4}[-. ]?[0-9]{4}|1[568][0-9]{2}[-. ]?[0-9]{4}", _A
)
_CTX_PHONE = re.compile(
    r"전화|연락|휴대|핸드폰|폰|팩스|문의|고객\s?센터|대표\s?번호|상담|콜센터|자택|직장|사무실|☎|✆|"
    r"\btel\b|\bphone|\bmobile|\bcell|\bh\.?p\b|\bfax\b|\bcall\b|\bcontact|\bph\b|\breach(?:ed)?\b|\bpager\b",
    _AI,
)


def _hhmm(v: str) -> bool:
    """A 24-hour clock time such as 0830, 1500 or 2400."""
    h, m = int(v[:2]), int(v[2:])
    return (h < 24 and m < 60) or v == "2400"


class PhoneRecognizer(_Recognizer):
    """Korean mobile, landline, VoIP/050X and 15XX/16XX/18XX numbers, plus +82 forms."""

    name = "kr_phone"
    entities = (E.PHONE,)

    def find(self, text: str, cfg: "Config | None" = None) -> list[Span]:
        if not _HAS_DIGIT.search(text):
            return self._spoken(text)
        out: list[Span] = []
        for m in _MOBILE_RX.finditer(text):
            seps = {(s or "").strip() for s in (m.group("s1"), m.group("s2"))}
            bare = m.group("s1") is None and m.group("s2") is None and text[m.start()] != "("
            if bare and _glued_alpha(text, m.start(), m.end()):
                continue
            score = 0.9 if seps & {"-", "."} else 0.85
            out.append(self._span(text, m.start(), m.end(), E.PHONE, score))
        for m in _LAND2_RX.finditer(text):
            ctx = _ctx(_CTX_PHONE, text, m.start(), m.end())
            score = 0.85 if (ctx or m.group("s1").startswith(")")) else 0.75
            out.append(self._span(text, m.start(), m.end(), E.PHONE, score))
        for rx in (_NANP_RX, _UK_MOBILE_RX):
            for m in rx.finditer(text):
                if _ctx(_CTX_PHONE, text, m.start(), m.end(), before=25):
                    out.append(self._span(text, m.start(), m.end(), E.PHONE, 0.8))
        out += self._spoken(text)
        for rx in (_INTL_RX, _PAREN_RX):
            for m in rx.finditer(text):
                out.append(self._span(text, m.start(), m.end(), E.PHONE, 0.9))
        for m in _LAND_RX.finditer(text):
            sep = m.group("s")
            ctx = _ctx(_CTX_PHONE, text, m.start(), m.end())
            if not sep:
                if _glued_alpha(text, m.start(), m.end()):
                    continue
                score = 0.85 if ctx else 0.55  # bare 9-11 digit codes also start with 0
            elif sep == " ":
                score = 0.85 if ctx else 0.7
            else:
                score = 0.85
            out.append(self._span(text, m.start(), m.end(), E.PHONE, score))
        for m in _BIZ_RX.finditer(text):
            score = self._business_score(text, m)
            if score:
                out.append(self._span(text, m.start(), m.end(), E.PHONE, score))
        return _dedupe(out)

    def _spoken(self, text: str) -> list[Span]:
        """"공일공에 삼사오육에 칠팔구공" written by transcription apps."""
        if "일" not in text:
            return []
        return [self._span(text, m.start(), m.end(), E.PHONE, 0.9) for m in _SPOKEN_PHONE_RX.finditer(text)]

    @staticmethod
    def _business_score(text: str, m: re.Match) -> float:
        """15XX-XXXX numbers collide with ranges ("1500-1800 kcal"); context decides."""
        if _ctx(_CTX_PHONE, text, m.start(), m.end()):
            return 0.9
        if m.group("s") not in ("-", ".") or _glued_alpha(text, m.start(), m.end()):
            return 0.0
        if _UNIT_AFTER.match(text, m.end()):
            return 0.0
        a, b = int(m.group("a")), int(m.group("b"))
        if 1500 <= b <= 2099 and b > a:  # looks like a numeric/year range
            return 0.0
        sa, sb = m.group("a"), m.group("b")
        if _hhmm(sa) and _hhmm(sb) and int(sa[2:]) % 5 == 0 and int(sb[2:]) % 5 == 0:
            return 0.0  # a shift / fasting time range: "E 1500-2300", "야간 1800-0600"
        return 0.75


# --------------------------------------------------------------------------- EMAIL / URL

_EMAIL_RX = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9][A-Za-z0-9._%+-]*@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*"
    r"\.[A-Za-z]{2,24}(?![A-Za-z0-9-])",
    _A,
)


# Spam-proofed addresses: "soyul.ahn 골뱅이 naver.com", "hana(at)hanmail.net",
# "hana [at] hanmail [dot] net", "hana @ hanmail.net".
_WEBMAIL = ("naver|daum|hanmail|gmail|googlemail|kakao|nate|yahoo|hotmail|outlook|icloud|live|msn|"
            "korea|empal|paran|chol|dreamwiz|hanmir|lycos|proton|protonmail|me")
_OB_AT = r"(?P<at>[ \t]*(?:@|골뱅이|\(at\)|\[at\]|\{at\}|<at>)[ \t]*|[ \t]+at[ \t]+)"
_OB_DOT = r"(?:\.|[ \t]*(?:\[dot\]|\(dot\)|\{dot\}|<dot>|점)[ \t]*|[ \t]+dot[ \t]+)"
_OB_TLD = r"(?:com|net|org|co|kr|ac|go|or|re|ne|jp|cn|io|edu|gov|info|biz|me)"
_OB_EMAIL_RX = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9][A-Za-z0-9._%+-]{0,63}" + _OB_AT
    + r"(?P<dom>[A-Za-z0-9-]{1,63})(?:" + _OB_DOT + r"[A-Za-z0-9-]{1,63}){0,3}?"
    + _OB_DOT + _OB_TLD + r"(?:" + _OB_DOT + _OB_TLD + r")?(?![A-Za-z0-9])",
    _AI,
)
_WEBMAIL_RX = re.compile(rf"(?:{_WEBMAIL})", _AI)
_OB_HINT = re.compile(r"@|골뱅이|at\b", _AI)


class EmailRecognizer(_Recognizer):
    name = "email"
    entities = (E.EMAIL,)

    def find(self, text: str, cfg: "Config | None" = None) -> list[Span]:
        out = [self._span(text, m.start(), m.end(), E.EMAIL, 0.95) for m in _EMAIL_RX.finditer(text)]
        if not _OB_HINT.search(text):
            return out
        for m in _OB_EMAIL_RX.finditer(text):
            at = m.group("at")
            if at == "@":
                continue  # a plain address: _EMAIL_RX above
            if at.strip().lower() == "at" and not _WEBMAIL_RX.fullmatch(m.group("dom")):
                continue  # "meet me at home.com" style prose needs a known mail domain
            out.append(self._span(text, m.start(), m.end(), E.EMAIL, 0.9))
        return _dedupe(out)


_URL_RX = re.compile(
    r"(?<![A-Za-z0-9@.])(?:https?://|www\.)[A-Za-z0-9\-._~:/?#\[\]@!$&()*+,;=%가-힣]+",
    _AI,
)
_TRAIL_PUNCT = ".,;:!?'\""
_HANGUL_RUN_END = re.compile(r"[가-힣]+$")
# Korean particles / copula endings that commonly follow a URL.
_PARTICLES = frozenset(
    "은 는 이 가 을 를 의 에 도 와 과 로 으로 에서 에서는 에게 께 만 랑 이랑 하고 이나 나 까지 "
    "부터 처럼 보다 로는 으로는 에는 에도 이다 입니다 이며 이고 이요 요 인데 이라 라".split()
)


def _trim_url(url: str) -> str:
    """Cut Hangul out of the host, then trailing punctuation, unbalanced brackets, particles."""
    host_end = url.find("/", url.find("//") + 2 if "//" in url else 0)
    host = url if host_end < 0 else url[:host_end]
    for i, ch in enumerate(host):
        if _is_hangul(ch):
            return _trim_url(url[:i]) if i else ""
    while True:
        before = url
        url = url.rstrip(_TRAIL_PUNCT)
        for close, open_ in ((")", "("), ("]", "[")):
            if url.endswith(close) and url.count(close) > url.count(open_):
                url = url[:-1]
        m = _HANGUL_RUN_END.search(url)
        if m and m.group() in _PARTICLES and m.start() > 0:
            url = url[:m.start()]
        if url == before:
            return url


class UrlRecognizer(_Recognizer):
    name = "url"
    entities = (E.URL,)

    def find(self, text: str, cfg: "Config | None" = None) -> list[Span]:
        out = []
        for m in _URL_RX.finditer(text):
            url = _trim_url(m.group())
            rest = url.split("//", 1)[1] if "//" in url else url[4:]
            if rest and ("//" in url or "." in rest):
                out.append(self._span(text, m.start(), m.start() + len(url), E.URL, 0.9))
        return out


# --------------------------------------------------------------------------- CARD

_CARD_RX = re.compile(
    _NB + r"(?:[0-9]{4}(?P<s1>[- ])[0-9]{4}(?P=s1)[0-9]{4}(?P=s1)[0-9]{4}(?:(?P=s1)[0-9]{1,3})?"
    r"|[0-9]{4}(?P<s2>[- ])[0-9]{6}(?P=s2)[0-9]{4,5}"
    r"|[0-9]{14,19})" + _NA,
    _A,
)
# Mastercard 51-55, Amex 34/37, Diners 30/36/38, JCB 35, Discover 60/64/65, UnionPay 62.
_IIN2 = frozenset("30 34 35 36 37 38 51 52 53 54 55 60 62 64 65".split())


def _iin_ok(ds: str) -> bool:
    """Plausible issuer prefix (also Visa 4, Korean domestic 9, Mastercard 2221-2720)."""
    return ds[0] in "49" or ds[:2] in _IIN2 or 2221 <= int(ds[:4]) <= 2720


class CardRecognizer(_Recognizer):
    """Payment cards: 14-19 digits in common groupings, Luhn required."""

    name = "card"
    entities = (E.CARD,)

    def find(self, text: str, cfg: "Config | None" = None) -> list[Span]:
        if not _HAS_DIGIT.search(text):  # every shape needs an ASCII digit
            return []
        out = []
        for m in _CARD_RX.finditer(text):
            start, raw = m.start(), m.group()
            sep = m.group("s1") or m.group("s2")
            if not sep and _glued_alpha(text, m.start(), m.end()):
                continue
            candidates = [raw]
            if m.group("s1") and raw.count(sep) == 4:
                # "4111 1111 1111 1111 3개월": a space-joined 5th group is usually another number
                short = raw[: raw.rfind(sep)]
                candidates = [short, raw] if sep == " " else [raw, short]
            for cand in candidates:
                ds = V.digits(cand)
                if _iin_ok(ds) and V.luhn_ok(ds):
                    score = 0.9 if sep else 0.8
                    out.append(self._span(text, start, start + len(cand), E.CARD, score))
                    break
        return out


# --------------------------------------------------------------------------- ACCOUNT

_DL_REGION_CODES = frozenset(str(n) for n in range(11, 27)) | {"28"}  # 11 서울 .. 26 울산, 28 경기북부
_ACCT_RX = re.compile(_NB + r"(?:[0-9]{2,6}(?:-[0-9]{2,7}){1,4}|[0-9]{10,14})" + _NA, _A)
# "계좌 110 482 339201 신한": single spaces between groups (only with context right before).
_ACCT_SPACED_RX = re.compile(_NB + r"[0-9]{2,6}(?: [0-9]{2,7}){1,4}" + _NA, _A)
_CTX_ACCT = re.compile(
    r"계좌|은행|입금|예금|송금|이체|뱅크|무통장|증권|환불|카뱅|케뱅|토뱅|케이뱅크|"
    r"국민(?!\s?건강|\s?연금|\s?보험|\s?행복|\s?임대)|"
    r"신한|농협|우체국|새마을|신협|수협|카카오|토스|씨티|\bsc\s?제일|\bibk\b|\bkb\b|\bnh\b|"
    r"account|\bacct\b|\bbank|\ba/c\b",
    _AI,
)
_DATE_PREFIX = re.compile(r"((?:19|20)[0-9]{2})-([0-9]{1,2})-([0-9]{1,2})(?:-|$)", _A)
_DL_SHAPE = re.compile(r"([0-9]{2})-[0-9]{2}-[0-9]{6}-[0-9]{2}", _A)
_CARD_SHAPE = re.compile(r"[0-9]{4}-[0-9]{4}-[0-9]{4}-[0-9]{4}", _A)
_RRN_SHAPE = re.compile(r"([0-9]{6})-?([0-9])[0-9]{6}", _A)


def _not_account(value: str) -> bool:
    """Shapes owned by other recognizers (phone, RRN, licence, card, date)."""
    if _PHONE_SHAPE.fullmatch(value):
        return True
    m = _RRN_SHAPE.fullmatch(value)
    if m and V.rrn_date_ok(m.group(1), m.group(2)):
        return True
    m = _DL_SHAPE.fullmatch(value)
    if m and m.group(1) in _DL_REGION_CODES:
        return True
    if _CARD_SHAPE.fullmatch(value) and V.luhn_ok(value):
        return True
    m = _DATE_PREFIX.match(value)
    return bool(m and V.is_valid_date(*(int(x) for x in m.groups())))


class AccountRecognizer(_Recognizer):
    """Bank account numbers (10-16 digits), only near banking context words."""

    name = "kr_account"
    entities = (E.ACCOUNT,)

    def find(self, text: str, cfg: "Config | None" = None) -> list[Span]:
        if not _HAS_DIGIT.search(text):  # every shape needs an ASCII digit
            return []
        out = []
        for m in _ACCT_RX.finditer(text):
            value = m.group()
            if not 10 <= len(V.digits(value)) <= 16 or _not_account(value):
                continue
            if "-" not in value and _glued_alpha(text, m.start(), m.end()):
                continue
            if _ctx(_CTX_ACCT, text, m.start(), m.end(), before=25):
                out.append(self._span(text, m.start(), m.end(), E.ACCOUNT, 0.85))
            elif _ctx(_CTX_ACCT, text, m.start(), m.end(), before=0, after=15):
                # Context only after ("... (우리은행)으로 입금") loses to a label right before it.
                out.append(self._span(text, m.start(), m.end(), E.ACCOUNT, 0.8))
        for m in (_ACCT_SPACED_RX.finditer(text) if " " in text else ()):
            value = m.group()
            if not 10 <= len(V.digits(value)) <= 16 or _not_account(value.replace(" ", "-")):
                continue
            if _ctx(_CTX_ACCT, text, m.start(), m.end(), before=15):
                out.append(self._span(text, m.start(), m.end(), E.ACCOUNT, 0.8))
        return _dedupe(out)


# --------------------------------------------------------------------------- PASSPORT

_PASSPORT_RX = re.compile(
    r"(?<![A-Za-z0-9])[MSRGDT](?:[0-9]{8}|[0-9]{3}[A-Z][0-9]{4})(?![A-Za-z0-9])", _AI
)
_CTX_PASSPORT = re.compile(r"여권|passport|\bpass\s?no", _AI)
# Any country's passport number right after its own label: "여권번호 C1234567", "Passport No. 123456789".
_PASSPORT_LABELLED_RX = re.compile(
    r"(?:여권[ \t]?(?:번호)?|\bpassport[ \t]?(?:no\.?|number|#)?|\bpass[ \t]?no\.?)"
    r"[ \t]*(?:[:：#=][ \t]*)?(?P<v>[A-Z]{0,2}[0-9]{6,9}[A-Z0-9]?)(?![A-Za-z0-9])(?![-.][0-9])",
    _AI,
)


class PassportRecognizer(_Recognizer):
    """Korean passports: M12345678 (old) and M123A4567 (2021+); prefix M/S/R/G/D/T."""

    name = "kr_passport"
    entities = (E.PASSPORT,)

    def find(self, text: str, cfg: "Config | None" = None) -> list[Span]:
        if not _HAS_DIGIT.search(text):  # every shape needs an ASCII digit
            return []
        out = []
        for m in _PASSPORT_RX.finditer(text):
            value = m.group()
            if _ctx(_CTX_PASSPORT, text, m.start(), m.end(), before=25):
                score = 0.9  # with context, a lower-case prefix is accepted too
            elif value != value.upper():
                continue
            else:
                score = 0.6 if value[1:].isdigit() else 0.75
            out.append(self._span(text, m.start(), m.end(), E.PASSPORT, score))
        for m in _PASSPORT_LABELLED_RX.finditer(text):
            out.append(self._span(text, m.start("v"), m.end("v"), E.PASSPORT, 0.85))
        return _dedupe(out)


# --------------------------------------------------------------------------- DRIVER_LICENSE

_DL_REGIONS = "서울|부산|경기남부|경기북부|경기|강원|충북|충남|전북|전남|경북|경남|제주|대구|인천|광주|대전|울산"
_DL_RX = re.compile(
    _NB + r"(?P<r>[0-9]{2})(?P<s>[- ]?)[0-9]{2}(?P=s)[0-9]{6}(?P=s)[0-9]{2}" + _NA, _A
)
_DL_REGION_RX = re.compile(
    rf"(?:{_DL_REGIONS})[ \t]?[0-9]{{2}}-[0-9]{{6}}-[0-9]{{2}}" + _NA, _A
)
_CTX_DL = re.compile(r"운전|면허|driver|licen[cs]e|\bd/?l\b", _AI)


class DriverLicenseRecognizer(_Recognizer):
    """운전면허번호: 11-12-345678-90 or 서울 12-345678-90."""

    name = "kr_driver_license"
    entities = (E.DRIVER_LICENSE,)

    def find(self, text: str, cfg: "Config | None" = None) -> list[Span]:
        if not _HAS_DIGIT.search(text):  # every shape needs an ASCII digit
            return []
        out = []
        for m in _DL_RX.finditer(text):
            if m.group("r") not in _DL_REGION_CODES:
                continue
            ctx = _ctx(_CTX_DL, text, m.start(), m.end(), before=25)
            if m.group("s") == "-":
                score = 0.95 if ctx else 0.85
            elif ctx and not _glued_alpha(text, m.start(), m.end()):
                score = 0.8
            else:
                continue
            out.append(self._span(text, m.start(), m.end(), E.DRIVER_LICENSE, score))
        for m in _DL_REGION_RX.finditer(text):
            score = 0.95 if _ctx(_CTX_DL, text, m.start(), m.end(), before=25) else 0.9
            out.append(self._span(text, m.start(), m.end(), E.DRIVER_LICENSE, score))
        return _dedupe(out)


# --------------------------------------------------------------------------- BRN

_BRN_RX = re.compile(_NB + r"[0-9]{3}(?P<s>[- ]?)[0-9]{2}(?P=s)[0-9]{5}" + _NA, _A)
_CTX_BRN = re.compile(
    r"사업자|\bbrn\b|business\s?(?:registration|reg|no|number)|tax\s?(?:id|registration)", _AI
)


class BrnRecognizer(_Recognizer):
    """사업자등록번호 123-45-67890 (checksum required; bare digits need context)."""

    name = "kr_brn"
    entities = (E.BRN,)

    def find(self, text: str, cfg: "Config | None" = None) -> list[Span]:
        if not _HAS_DIGIT.search(text):  # every shape needs an ASCII digit
            return []
        out = []
        for m in _BRN_RX.finditer(text):
            if not V.brn_checksum_ok(m.group()):
                continue
            ctx = _ctx(_CTX_BRN, text, m.start(), m.end(), before=25)
            if m.group("s") == "-":
                score = 0.95 if ctx else 0.9
            elif ctx and not _glued_alpha(text, m.start(), m.end()):
                score = 0.85
            else:
                continue
            out.append(self._span(text, m.start(), m.end(), E.BRN, score))
        return out


# --------------------------------------------------------------------------- HEALTH_INSURANCE

_HI_RX = re.compile(_NB + r"[0-9](?:-?[0-9]){9,10}" + _NA, _A)
_CTX_HI = re.compile(
    r"건강\s?보험|보험\s?증|증\s?번호|보험\s?번호|건보|피보험|의료\s?급여|"
    r"health\s?insurance|insurance\s?(?:no|number|id)",
    _AI,
)


# The number's own label right before it: "건강보험증번호: 0113626399".
_HI_LABEL_BEFORE = re.compile(
    r"(?:건강\s?보험\s?증?\s?번호|보험\s?증\s?번호|피보험자\s?번호|insurance\s?(?:no|number|id))"
    r"\.?[ \t]*[:：]?[ \t]*$",
    _AI,
)


class HealthInsuranceRecognizer(_Recognizer):
    """건강보험증번호: 10-11 digits, only after insurance context words."""

    name = "kr_health_insurance"
    entities = (E.HEALTH_INSURANCE,)

    def find(self, text: str, cfg: "Config | None" = None) -> list[Span]:
        if not _HAS_DIGIT.search(text):  # every shape needs an ASCII digit
            return []
        out = []
        for m in _HI_RX.finditer(text):
            value = m.group()
            if _glued_alpha(text, m.start(), m.end()):
                continue
            if _PHONE_SHAPE.fullmatch(value):
                # Phone-shaped: only a bare digit run directly after its own label is insurance.
                labelled = value.isdigit() and _HI_LABEL_BEFORE.search(text, max(0, m.start() - 20), m.start())
                if labelled:
                    out.append(self._span(text, m.start(), m.end(), E.HEALTH_INSURANCE, 0.9))
                continue
            if _ctx(_CTX_HI, text, m.start(), m.end(), before=20):
                out.append(self._span(text, m.start(), m.end(), E.HEALTH_INSURANCE, 0.85))
        return out


# --------------------------------------------------------------------------- MRN

_MRN_RX = re.compile(
    r"(?P<label>등록\s?번호|차트\s?(?:번호|no\.?|#)?|환자\s?(?:번호|id|no\.?|#)|병록\s?번호|"
    r"의무\s?기록\s?번호|진료\s?카드\s?번호|환번|외래\s?번호|입원\s?번호|임시\s?번호|원무\s?번호|"
    r"\bchart\s?(?:no\.?|number|#|id)|\bmrn\b|\bm\.r\.n\.?|"
    r"\breg(?:istration)?\.?\s?(?:no\.?|number|#)|\bunit\s?(?:no\.?|number|#)|\br#|"
    r"\bmedical\s?record\s?(?:no\.?|number|#)|\bpt\.?\s?(?:id|no\.?|#)|"
    r"\bpatient\s?(?:id|no\.?|number|#)|\bhospital\s?(?:id|no\.?|number)|\bid\b)"
    r"(?:[ \t]*\([^)\n]{0,15}\))?[ \t]*(?:[:：#=.][ \t]*)?(?:\r?\n[ \t]*)?"
    r"(?P<v>[A-Za-z]{0,3}-?[0-9]{3,12}(?:-[0-9]{1,8})?)(?![A-Za-z0-9])(?![-.][0-9])",
    _AI,
)
# "주민등록번호", "외국인 등록번호", "사업자등록번호" ... are not hospital registration numbers.
_MRN_NOT_BEFORE = re.compile(r"(?:주민|외국인|사업자|법인|차량|자동차|면허|장애인|우편|여권)[ \t]?$")
# "차트 2024년 정리", "차트 1234장": a count or year, not a chart number.
_MRN_UNIT_AFTER = re.compile(r"[ \t]?(?:년|월|일|세|개월|개|장|건|회|명|번째|시간|분|페이지|쪽|원)")


# "(M/52, #20934411)": a '#' number right after sex/age in a patient list entry.
_MRN_HASH_RX = re.compile(
    r"(?:[MF][ ]?/[ ]?[0-9]{1,3}|[0-9]{1,3}[ ]?/[ ]?[MF]|[0-9]{1,3}[ ]?세|남|여)[ ]*[,;/][ ]*"
    r"#[ ]?(?P<v>[0-9]{5,10})(?![0-9])",
    _A,
)


class MrnRecognizer(_Recognizer):
    """Hospital registration / chart numbers after a label; the span is the ID value only."""

    name = "mrn"
    entities = (E.MRN,)

    def find(self, text: str, cfg: "Config | None" = None) -> list[Span]:
        if not _HAS_DIGIT.search(text):  # every shape needs an ASCII digit
            return []
        out = []
        for m in _MRN_RX.finditer(text):
            value, label = m.group("v"), m.group("label")
            if not 4 <= len(V.digits(value)) <= 14:
                continue
            if _MRN_NOT_BEFORE.search(text, max(0, m.start() - 6), m.start()):
                continue
            rrn = _RRN_SHAPE.fullmatch(value)
            if rrn and V.rrn_date_ok(rrn.group(1), rrn.group(2)):
                continue
            if _MRN_UNIT_AFTER.match(text, m.end("v")):
                continue
            score = 0.7 if label.lower() == "id" else 0.9
            out.append(self._span(text, m.start("v"), m.end("v"), E.MRN, score))
        for m in (_MRN_HASH_RX.finditer(text) if "#" in text else ()):
            out.append(self._span(text, m.start("v"), m.end("v"), E.MRN, 0.8))
        return _dedupe(out)


# --------------------------------------------------------------------------- MEDICAL_LICENSE

# 의사/치과의사/한의사/간호사/약사 면허번호, 전문의 자격번호. Driving licences are excluded.
_MEDLIC_RX = re.compile(
    r"(?P<label>(?:의사|치과\s?의사|한의사|간호사|조산사|약사|한약사|의료인|보건의료인)?\s?면허\s?(?:번호|no\.?|#)?|"
    r"전문의\s?(?:자격\s?)?(?:번호|증\s?번호)|자격\s?번호|\bmedical\s?licen[cs]e\s?(?:no\.?|number|#)?|"
    r"\bGMC(?:\s?(?:no\.?|number|#))?|\bNPI(?:\s?(?:no\.?|number|#))?)"
    r"[ \t]*(?:[:：#=.][ \t]*)?(?:제[ \t]*)?(?P<v>[0-9]{3,7})(?![0-9])(?:[ \t]*호)?",
    _AI,
)
_MEDLIC_NOT_BEFORE = re.compile(r"(?:운전|자동차|이륜차|사업|영업|통신|무선)[ \t]?$")


class MedicalLicenseRecognizer(_Recognizer):
    """Health professional licence numbers like '면허번호 제12345호'; the span is the number only."""

    name = "kr_medical_license"
    entities = (E.MEDICAL_LICENSE,)

    def find(self, text: str, cfg: "Config | None" = None) -> list[Span]:
        if not _HAS_DIGIT.search(text):  # every shape needs an ASCII digit
            return []
        out = []
        for m in _MEDLIC_RX.finditer(text):
            label = m.group("label")
            if _MEDLIC_NOT_BEFORE.search(text, max(0, m.start() - 6), m.start()):
                continue
            if "운전" in label:
                continue
            if _MRN_UNIT_AFTER.match(text, m.end("v")):
                continue  # "의사 면허 2010년 취득": a year, not a licence number
            out.append(self._span(text, m.start("v"), m.end("v"), E.MEDICAL_LICENSE, 0.9))
        return out


# --------------------------------------------------------------------------- IP

_IPV4_RX = re.compile(
    r"(?<![0-9A-Za-z_.])[0-9]{1,3}(?:\.[0-9]{1,3}){3}(?![0-9A-Za-z_])(?!\.[0-9])", _A
)
_IPV6_RX = re.compile(
    r"(?<![0-9A-Za-z])(?:[0-9A-Fa-f]{1,4})?(?::(?:[0-9A-Fa-f]{1,4})?){2,8}(?![0-9A-Za-z:])", _A
)
_CTX_IP = re.compile(r"\bip|아이피|서버|server|host|호스트|접속|address|주소", _AI)
_CTX_VERSION = re.compile(r"버전|version|\bver\b|\bv\s?$|\brev\b|§|section", _AI)


class IpRecognizer(_Recognizer):
    """IPv4 (valid octets, not part of longer dotted runs) and IPv6."""

    name = "ip"
    entities = (E.IP,)

    def find(self, text: str, cfg: "Config | None" = None) -> list[Span]:
        out = []
        for m in _IPV4_RX.finditer(text):
            value = m.group()
            if not V.ipv4_ok(value) or _ctx(_CTX_VERSION, text, m.start(), m.end(), before=12):
                continue
            ctx = _ctx(_CTX_IP, text, m.start(), m.end(), before=20)
            if not ctx and all(len(p) == 1 for p in value.split(".")):
                continue  # "1.2.3.4" is far more often a heading number than an address
            out.append(self._span(text, m.start(), m.end(), E.IP, 0.95 if ctx else 0.85))
        for m in _IPV6_RX.finditer(text):
            value = m.group()
            groups = [g for g in value.split(":") if g]
            if len(groups) < 2 or sum(map(len, groups)) < 4 or not V.ipv6_ok(value):
                continue
            out.append(self._span(text, m.start(), m.end(), E.IP, 0.85))
        return out


# --------------------------------------------------------------------------- PLATE

_PLATE_LETTERS = "가나다라마거너더러머버서어저고노도로모보소오조구누두루무부수우주아바사자배하허호"
_PLATE_REGIONS = "서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|전북|전남|경북|경남|제주"
_PLATE_RX = re.compile(
    rf"(?:(?P<reg>{_PLATE_REGIONS})[ \t]?)?(?<![0-9])[0-9]{{2,3}}[{_PLATE_LETTERS}]"
    r"(?P<sp>[ \t]?)[0-9]{4}(?![0-9])(?![-.][0-9])",
    _A,
)
_CTX_PLATE = re.compile(r"차량|번호판|차번|자동차|주차|plate|vehicle|\bcar\b", _AI)


def _temperature_or_room_time(value: str) -> bool:
    """'<34-42>도 ...' or '<n>도/호/주 <HHMM>' in nursing shorthand."""
    m = re.search(r"([0-9]{2,3})([도호주])[ \t]?([0-9]{4})$", value)
    if not m:
        return False
    if m.group(2) == "도" and 34 <= int(m.group(1)) <= 42:
        return True
    return _hhmm(m.group(3))


class PlateRecognizer(_Recognizer):
    """Vehicle plates: 12가3456, 123가4567, 서울12가3456."""

    name = "kr_plate"
    entities = (E.PLATE,)

    def find(self, text: str, cfg: "Config | None" = None) -> list[Span]:
        if not _HAS_DIGIT.search(text):  # every shape needs an ASCII digit
            return []
        out = []
        for m in _PLATE_RX.finditer(text):
            ctx = _ctx(_CTX_PLATE, text, m.start(), m.end(), before=20)
            if m.group("sp"):
                if _UNIT_AFTER.match(text, m.end()) and not ctx:
                    continue  # "37도 2000년", "32주 1500g"
                if not ctx and _temperature_or_room_time(m.group()):
                    continue  # "체온 39도 1400", "501호 0830": a reading or room plus a clock time
                score = 0.9 if ctx else 0.7
            else:
                score = 0.9 if (ctx or m.group("reg")) else 0.85
            out.append(self._span(text, m.start(), m.end(), E.PLATE, score))
        return out


# --------------------------------------------------------------------------- DATE / BIRTHDATE

_Y4 = r"(?:19|20)[0-9]{2}"
_DNB = r"(?<![0-9])(?<![0-9][-./])"
_DNB_ALNUM = r"(?<![0-9A-Za-z])(?<![0-9][-./])"
_DNA = r"(?![0-9])(?![-./][0-9])"
_MON = (r"(?P<mon>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|"
        r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_KO_MD = r"(?P<m>[0-9]{1,2})[ \t]?월[ \t]?(?P<d>[0-9]{1,2})[ \t]?일"


def _drx(pattern: str, flags: int = _A) -> re.Pattern:
    return re.compile(pattern, flags)


# (regex, score, needs a birth keyword or a 생 suffix)
_DATE_RXS: list[tuple[re.Pattern, float, bool]] = [
    # 2024-03-15, 2024.03.15, 2024/03/15
    (_drx(_DNB + rf"(?P<y>{_Y4})(?P<s>[-./])(?P<m>[0-9]{{1,2}})(?P=s)(?P<d>[0-9]{{1,2}})" + _DNA),
     0.85, False),
    # 2024. 3. 15.
    (_drx(_DNB + rf"(?P<y>{_Y4})\.[ \t]+(?P<m>[0-9]{{1,2}})\.[ \t]+(?P<d>[0-9]{{1,2}})(?![0-9])"),
     0.85, False),
    # 2024년 3월 (15일)
    (_drx(_DNB + rf"(?P<y>{_Y4})[ \t]?년[ \t]?(?P<m>[0-9]{{1,2}})[ \t]?월"
          r"(?:[ \t]?(?P<d>[0-9]{1,2})[ \t]?일)?"), 0.85, False),
    # 24년 3월 15일
    (_drx(_DNB + r"(?P<yy>[0-9]{2})[ \t]?년[ \t]?" + _KO_MD), 0.8, False),
    # 24.03.15
    (_drx(_DNB_ALNUM + r"(?P<yy>[0-9]{2})\.(?P<m>[0-9]{2})\.(?P<d>[0-9]{2})" + _DNA), 0.7, False),
    # March 15, 2024 / 15 Mar 2024
    (_drx(rf"\b{_MON}\.?[ \t]+(?P<d>[0-9]{{1,2}})(?:st|nd|rd|th)?,?[ \t]+(?P<y>{_Y4})\b", _AI),
     0.85, False),
    (_drx(rf"\b(?P<d>[0-9]{{1,2}})(?:st|nd|rd|th)?[ \t]+{_MON}\.?,?[ \t]+(?P<y>{_Y4})\b", _AI),
     0.85, False),
    # 15-Mar-1985, 15/Mar/1985, 15.Mar.1985
    (_drx(rf"(?<![0-9A-Za-z])(?P<d>[0-9]{{1,2}})(?P<s>[-/.]){_MON}(?P=s)(?P<y>{_Y4})\b", _AI),
     0.85, False),
    # Birth-only shapes: 19850302, 850302, 03/15/1985 (either d/m order), 85-03-02, 3월 2일
    (_drx(_DNB_ALNUM + rf"(?P<y>{_Y4})(?P<m>0[1-9]|1[0-2])(?P<d>[0-3][0-9])" + _DNA), 0.85, True),
    (_drx(_DNB_ALNUM + r"(?P<yy>[0-9]{2})(?P<m>0[1-9]|1[0-2])(?P<d>[0-3][0-9])" + _DNA), 0.8, True),
    (_drx(_DNB + rf"(?P<a>[0-9]{{1,2}})(?P<s>[-./])(?P<b>[0-9]{{1,2}})(?P=s)(?P<y>{_Y4})" + _DNA),
     0.85, True),
    (_drx(_DNB + r"(?P<yy>[0-9]{2})(?P<s>[-/])(?P<m>[0-9]{1,2})(?P=s)(?P<d>[0-9]{1,2})" + _DNA),
     0.8, True),
    (_drx(_DNB + _KO_MD), 0.8, True),
    # 85.3.15 (single-digit month/day): only as a birth date
    (_drx(_DNB_ALNUM + r"(?P<yy>[0-9]{2})\.(?P<m>[0-9]{1,2})\.(?P<d>[0-9]{1,2})" + _DNA), 0.8, True),
]
_YEAR_SAENG_RX = _drx(_DNB + r"(?:(?:19|20)[0-9]{2}|[0-9]{2})[ \t]?년[ \t]?생")
_BIRTH_KW = re.compile(
    r"생년월일|(?<!발)(?<!재)생년|(?<!발)(?<!재)(?<!회)생일|출생|탄생|"
    r"\bd\.?o\.?b\b|birth|\bborn\b|\bb-?day\b",
    _AI,
)
_OTHER_DATE_KW = re.compile(
    r"내원|진료|입원|퇴원|수술|검사|방문|작성|발행|접수|시행|처방|투약|채혈|촬영|예약|사망|진단|"
    r"발병|발생|등록|가입|시작|종료|기준|admi|discharg|visit|exam|report|\bdos\b|service",
    _AI,
)
_SAENG_FOLLOW = "이인으의은도과와며고"
# The birth keyword works as a label for the date: "생년월일: ", "생일은 ", "출생일 ", "DOB: ".
_BIRTH_LABEL_GAP = re.compile(r"[ \t]*(?:일|일자|년월일)?[ \t]*(?:은|는|이|가|:|：|=|-|\(|/)?[ \t]*\Z")
# Words between a birth keyword and a date that make it some other date ("출생 후 2024.03.20").
_NOT_BIRTH_GAP = re.compile(r"후|이후|뒤|전|이전|미상|모름|불명|체중|키|신장|\bweight\b|\bafter\b", _AI)
# Events written right after a date ("2024-03-05 퇴원", "2024.03.20 BCG 접종").
_EVENT_AFTER = re.compile(
    r"\A[ \t,)]*(?:[A-Za-z0-9]{1,6}[ \t]+)?(?:내원|진료|입원|퇴원|수술|검사|방문|접종|시행|처방|투약|투여|"
    r"채혈|촬영|예약|사망|진단|발병|발생|등록|혈당|측정|복용|발급|상승|저하|외래|재진|초진)"
)
# "주민번호 앞자리 870204", "주민번호 앞 6자리 870204": the RRN front half is the birth date.
_RRN_FRONT_RX = re.compile(
    r"(?:앞[ \t]?(?:자리|번호|부분|6[ \t]?자리))[ \t]*(?:[:：=]|은|는|이|가|숫자는?)?(?:[ \t]+[가-힣]{1,4})?[ \t]*"
    r"(?P<v>[0-9]{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12][0-9]|3[01]))(?![0-9])(?![-.][0-9])",
    _A,
)
_CTX_RRN_FRONT = re.compile(r"주민|외국인[ \t]?등록|등록[ \t]?번호|\brrn\b|\bssn\b|뒷[ \t]?자리|뒤[ \t]?7", _AI)


def _saeng_at(text: str, pos: int) -> bool:
    """True if text[pos] is the birth suffix 생/生 (not 생검, 생활 ...)."""
    if pos >= len(text) or text[pos] not in "생生":
        return False
    nxt = text[pos + 1] if pos + 1 < len(text) else ""
    return not _is_hangul(nxt) or nxt in _SAENG_FOLLOW


def _saeng_after(text: str, end: int) -> int:
    """Length of a birth suffix right after a date: "생", "生" or " 생" (a lone word)."""
    if _saeng_at(text, end):
        return 1
    if end < len(text) and text[end] == " " and _saeng_at(text, end + 1):
        nxt = text[end + 2] if end + 2 < len(text) else ""
        return 2 if not _is_hangul(nxt) else 0
    return 0


def _date_valid(m: re.Match) -> bool:
    gd = m.groupdict()
    if gd.get("a") is not None:  # d/m/yyyy or m/d/yyyy: either reading is fine
        a, b, y = int(gd["a"]), int(gd["b"]), int(gd["y"])
        return V.is_valid_date(y, a, b) or V.is_valid_date(y, b, a)
    mon = gd.get("mon")
    month = _MONTHS[mon[:3].lower()] if mon else int(gd["m"])
    if gd.get("d") is None:
        return 1 <= month <= 12
    day = int(gd["d"])
    if gd.get("y") is not None:
        return V.is_valid_date(int(gd["y"]), month, day)
    if gd.get("yy") is not None:
        return V.is_valid_yy_date(int(gd["yy"]), month, day)
    return V.is_valid_date(2000, month, day)  # month/day only (leap-tolerant)


def _birth_context(text: str, start: int, end: int, lo: int) -> bool:
    """A birth keyword within 15 chars before `start` (not before `lo`), not overridden.

    When words other than a label separator stand between the keyword and the date, an event
    right after the date ("출생 체중 3.2kg, 2024-03-05 퇴원") or a word like 후/미상 in between
    ("출생 후 2024.03.20") makes it an ordinary date.
    """
    last = None
    for last in _BIRTH_KW.finditer(text, max(lo, start - 15), start):
        pass
    if last is None or _OTHER_DATE_KW.search(text, last.end(), start):
        return False
    gap = text[last.end():start]
    if _BIRTH_LABEL_GAP.match(gap):
        return True
    if _NOT_BIRTH_GAP.search(gap):
        return False
    return not _EVENT_AFTER.match(text[end:end + 16])


class DateRecognizer(_Recognizer):
    """Dates (DATE) and birth dates (BIRTHDATE: birth keyword nearby or a 생 suffix)."""

    name = "date"
    entities = (E.BIRTHDATE, E.DATE)

    def find(self, text: str, cfg: "Config | None" = None) -> list[Span]:
        if not _HAS_DIGIT.search(text):
            return self._spoken(text)
        cands: list[tuple[int, int, float, bool]] = []
        for rx, score, needs_ctx in _DATE_RXS:
            for m in rx.finditer(text):
                if _date_valid(m):
                    cands.append((m.start(), m.end(), score, needs_ctx))
        for m in _YEAR_SAENG_RX.finditer(text):
            if _saeng_at(text, m.end() - 1):
                cands.append((m.start(), m.end() - 1, 0.85, False))
        cands.sort()
        out: list[Span] = []
        lo, pending = 0, []  # a birth keyword only counts after the previous date ended
        for start, end, score, needs_ctx in cands:
            lo = max([lo] + [e for e in pending if e <= start])
            pending = [e for e in pending if e > start] + [end]
            saeng = _saeng_after(text, end)
            if saeng:
                out.append(self._span(text, start, end + saeng, E.BIRTHDATE, max(score, 0.85)))
            elif _birth_context(text, start, end, lo):
                out.append(self._span(text, start, end, E.BIRTHDATE, 0.9))
            elif not needs_ctx:
                out.append(self._span(text, start, end, E.DATE, score))
        for m in (_RRN_FRONT_RX.finditer(text) if "앞" in text else ()):
            if V.rrn_date_ok(m.group("v")) and _ctx(_CTX_RRN_FRONT, text, m.start(), m.end(),
                                                      before=30, after=30):
                out.append(self._span(text, m.start("v"), m.end("v"), E.BIRTHDATE, 0.9))
        out += self._spoken(text)
        return _dedupe(out)

    def _spoken(self, text: str) -> list[Span]:
        """"팔칠년 오월 이십일일" (Hangul numerals) after a birth question."""
        if "년" not in text or "월" not in text:
            return []
        return [self._span(text, m.start(), m.end(), E.BIRTHDATE, 0.8) for m in _SPOKEN_BIRTH_RX.finditer(text)
                if _ctx(_CTX_SPOKEN_BIRTH, text, m.start(), m.end(), before=60)]


def _expand_yy(yy: int, today_year: int | None = None) -> int:
    """Two-digit year -> four digits; years after the current one belong to the 1900s."""
    current = (today_year or datetime.date.today().year) % 100
    return 2000 + yy if yy <= current else 1900 + yy


def parse_date(value: str) -> tuple[int | None, int | None, int | None]:
    """(year, month, day) of a DATE/BIRTHDATE span text such as "85.03.02생" or "2024년 3월".

    Unknown parts are None (e.g. "3월 2일" has no year; "03/04/1985" has no unambiguous month).
    """
    v = value.strip()
    if v.endswith(("생", "生")):
        v = v[:-1].rstrip()
    m = re.fullmatch(r"([0-9]{2}|[0-9]{4})[ \t]?년", v)
    if m:
        y = int(m.group(1))
        return (y if y >= 100 else _expand_yy(y)), None, None
    for rx, _score, _needs_ctx in _DATE_RXS:
        m = rx.fullmatch(v)
        if not m or not _date_valid(m):
            continue
        gd = m.groupdict()
        year = None
        if gd.get("y"):
            year = int(gd["y"])
        elif gd.get("yy"):
            year = _expand_yy(int(gd["yy"]))
        if gd.get("a") is not None:
            a, b = int(gd["a"]), int(gd["b"])
            if a > 12 or b > 12:
                return year, min(a, b), max(a, b)
            return year, None, None
        month = _MONTHS[gd["mon"][:3].lower()] if gd.get("mon") else int(gd["m"])
        return year, month, int(gd["d"]) if gd.get("d") else None
    return None, None, None


# --------------------------------------------------------------------------- reference numbers (OTHER)

# Insurance policy / claim numbers and employee IDs after their own label. The value needs at
# least 4 digits, so "접수번호 15" (a queue ticket) is left alone.
_REF_ID_RX = re.compile(
    r"(?P<label>(?:보험[ \t]?)?증권[ \t]?번호|보험[ \t]?계약[ \t]?번호|계약[ \t]?번호|"
    r"(?:사고[ \t]?)?접수[ \t]?번호|청구[ \t]?번호|사고[ \t]?번호|보상[ \t]?번호|"
    r"사번|직원[ \t]?번호|사원[ \t]?번호|교직원[ \t]?번호|학번|"
    r"\bpolicy[ \t]?(?:no\.?|number|#)|\bclaim[ \t]?(?:no\.?|number|#)|\bemployee[ \t]?(?:id|no\.?|number|#))"
    r"[ \t]*(?:[:：#=][ \t]*)?(?P<v>[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){4,24})(?![A-Za-z0-9-])",
    _AI,
)


class ReferenceIdRecognizer(_Recognizer):
    """증권번호 / 사고접수번호 / 청구번호 / 사번 values (entity OTHER, redacted)."""

    name = "kr_reference_id"
    entities = (E.OTHER,)

    def find(self, text: str, cfg: "Config | None" = None) -> list[Span]:
        if not _HAS_DIGIT.search(text):  # every shape needs an ASCII digit
            return []
        out = []
        for m in _REF_ID_RX.finditer(text):
            value = m.group("v")
            if len(V.digits(value)) < 4 or _MRN_UNIT_AFTER.match(text, m.end("v")):
                continue
            out.append(self._span(text, m.start("v"), m.end("v"), E.OTHER, 0.85))
        return out


# --------------------------------------------------------------------------- spoken numbers

# Speech-to-text transcripts write digits as Sino-Korean words. Only two narrow shapes are
# read: a mobile number that starts with "공일공/영일영" (010) and a birth date in words after
# a birth question ("생년월일 ... 팔칠년 오월 이십일일").
_SD = "공영일이삼사오육칠팔구"
_SP_SEP = r"(?:[ \t]*(?:에|-|,|\.)[ \t]*|[ \t]+)?"
_SPOKEN_PHONE_RX = re.compile(
    rf"(?<![가-힣])[공영]일[공영육칠팔구]{_SP_SEP}[{_SD}]{{3,4}}{_SP_SEP}[{_SD}]{{4}}"
)
_SK_UNIT = "일이삼사오육칠팔구"
_SPOKEN_MONTH = r"(?:십[일이]?|[일이삼사오육유칠팔구시])월"
_SPOKEN_DAY = rf"(?:[이삼]?십[{_SK_UNIT}]?|[{_SK_UNIT}])일"
_SPOKEN_BIRTH_RX = re.compile(
    rf"(?<![가-힣])(?:(?:천[ \t]?구백|이천)[ \t]?)?(?:[{_SK_UNIT}]?십[{_SK_UNIT}]?|[{_SD}]{{2}})[ \t]?년"
    rf"[ \t]?{_SPOKEN_MONTH}(?:[ \t]?{_SPOKEN_DAY})?"
)
_CTX_SPOKEN_BIRTH = re.compile(r"생년월일|생일|출생|태어|\bdob\b|birth", _AI)


# --------------------------------------------------------------------------- assembly

BUILTIN_RECOGNIZERS = (
    RrnRecognizer, PhoneRecognizer, EmailRecognizer, UrlRecognizer, CardRecognizer,
    AccountRecognizer, PassportRecognizer, DriverLicenseRecognizer, BrnRecognizer,
    HealthInsuranceRecognizer, MrnRecognizer, MedicalLicenseRecognizer, IpRecognizer, PlateRecognizer,
    DateRecognizer, ReferenceIdRecognizer,
)


def custom_recognizers(cfg: "Config") -> list[RegexRecognizer]:
    """RegexRecognizers for cfg.custom_patterns; a bad regex raises ValueError."""
    out = []
    for i, p in enumerate(cfg.custom_patterns):
        if not cfg.is_enabled(p.entity):
            continue
        try:
            out.append(RegexRecognizer(f"custom_{i}_{p.entity.lower()}", p.entity, p.regex,
                                       score=p.score, context=p.context))
        except re.error as exc:
            raise ValueError(f"invalid regex in custom pattern for {p.entity!r}: {exc}") from exc
    return out


def pattern_recognizers(cfg: "Config") -> list["Recognizer"]:
    """All built-in pattern recognizers with an enabled entity, plus cfg.custom_patterns."""
    recs: list = [cls() for cls in BUILTIN_RECOGNIZERS]
    recs = [r for r in recs if any(cfg.is_enabled(e) for e in r.entities)]
    return recs + custom_recognizers(cfg)
