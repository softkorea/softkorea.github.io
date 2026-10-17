# bench — 비식별화 성능 재기

이 폴더에는 두 가지 측정이 있습니다.

| 파일 | 무엇을 재나 | 주의 |
|---|---|---|
| `corpus.jsonl`, `make_corpus.py`, `run_bench.py` | deid 자체 합성 코퍼스(240문서)에서 탐지 위치(span)의 정밀도·재현율 | deid 규칙을 이 코퍼스에 맞춰 고쳐 왔기 때문에 규칙만으로 약 99%가 나옵니다. **새 기능이 정말 도움이 되는지 판단하는 데는 쓰지 마세요.** |
| `fresh_a.jsonl`, `fresh_b.jsonl`, `score_values.py` | deid를 **맞춰 고치지 않은** 코퍼스(95문서, 정답 값 449개)에서 값 단위 누출 | 이 문서의 주제. Strata(진료실 PC의 LLM) 탐지를 켰을 때 실제로 나아지는지 확인합니다. |

모든 사람·번호·주소는 지어낸 것입니다.

---

## 1. fresh_a / fresh_b 코퍼스

- deid와 Microsoft Presidio를 비교하려고(2026-10) deid를 만들지 않은 별도 에이전트들이 쓴 가상 진료 문서입니다.
  deid 개발 과정에서 이 문서들을 보고 규칙을 고친 적이 없습니다 (held-out).
- `fresh_a.jsonl` 54문서(외래 기록, 의뢰서, 진단서, 보호자·직원 단톡, 녹취 STT, 엑셀 붙여넣기, 처방 메모 …),
  `fresh_b.jsonl` 41문서(한영 혼용 퇴원요약, 영문 의뢰서, 외국인 환자 접수, 의사 개인 메모, 민원, 전산 장애 메모 …).
  언어: 한국어 67, 한영 혼용 20, 영어 8.
- 한 줄에 문서 하나:

  ```json
  {"id": "fa-001", "genre": "soap_note", "lang": "ko", "text": "...",
   "pii": [{"value": "박정순", "entity": "PERSON"}, ...],
   "keep": ["2025-03-14", "HbA1c 7.9%", ...]}
  ```

  `pii`는 가려져야 할 정답 값, `keep`은 바뀌면 안 되는 임상 정보(진료 날짜, 약 용량, 검사 수치, 상병 코드)입니다.

**저장소에 넣으면서 바꾼 값** (실제 인물·회사 번호·가정 주소처럼 보일 수 있는 것만, 정답과 본문을 함께 바꿈):

- 유명인과 이름이 같거나 그렇게 읽힐 수 있는 인물 4명 → 다른 가상 이름 (영문 이름·이메일 포함 5개 표기)
- 기관·가정 유선번호 9개 → 국번 첫 자리가 1인 번호 (`02-1601-…`; 한국에서는 배정되지 않음), 해외 번호 2개 → 미배정·영상물용 예약 대역
- 자택·숙소·기숙사 주소 22개 → 지어낸 도로명·동 이름·단지 이름 (시·도·구 이름은 실제)
- 이메일 21개 → `example.com/.net/.org` 또는 `.example` 도메인 (RFC 2606 예약 도메인)
- 공인 IP 3개 → 문서용 예약 대역(RFC 5737), Luhn 검사를 통과하던 카드번호 2개 → 공개 테스트 카드번호,
  검증번호가 맞던 주민번호 1개 → 끝자리 변경

바꾼 뒤에도 규칙만 쓴 deid의 결과는 비교 당시와 값 하나까지 같습니다 (누출 68개, 과잉 변경 9개 동일).
차량번호·계좌·여권번호 같은 나머지 무작위 번호는 우연히 실제 번호와 겹칠 수 있습니다.

---

## 2. score_values.py 가 재는 것

```sh
python bench/score_values.py CORPUS.jsonl [CORPUS2 ...] [--config deid.toml] [--json OUT] [--verbose] [--limit N]
```

- 탐지기(`Detector`)를 한 번 만든 뒤 문서마다 `deid.api.deidentify(text, cfg, Vault(), detector=det)`를
  부릅니다 (`deid text`·`deid clip`과 같은 처리). `--config`가 없으면 **내장 기본값**(규칙만)을 씁니다.
  이때 `~/.config/deid/config.toml`과 `DEID_CONFIG`는 읽지 않습니다.
- **누출(leaked)**: 정답 값이 결과에 글자 그대로 남아 있으면 누출입니다. 숫자가 6자리 이상인 값은 결과 속
  숫자·공백·점·하이픈이 이어진 부분에서 구분자를 빼고 같은 숫자열이 보여도 누출로 칩니다
  (`010 4827 3391`은 정답 `010-4827-3391`의 누출, `010-****-3391`은 보호). 전각 숫자(`０１０`)는 반각으로 바꿔서 봅니다.
  같은 문서의 같은 값은 한 번만 셉니다.
- **보호율(recall)** = 보호된 값 / 평가한 값. 항목별, 코퍼스별, 언어별, 전체.
- **keep 보존**: `keep` 문자열 중 결과에 그대로 남은 비율.
- **과잉 변경(spurious)**: 원문과 결과를 낱말·공백·문장부호 단위로 맞대어(difflib, autojunk=False) 바뀐 구간 중
  정답 값과 전혀 닿지 않는 구간의 수. 개인정보가 아닌 곳을 가린 것(진료 날짜, 약 이름, 머리글)입니다.
- **탐지 spans**: deid 보고서의 항목별 처리 개수(가린 것 + 그대로 둔 DATE·ORG 포함, 같은 값이 여러 번 나오면 여러 번).
  정답 수보다 훨씬 많으면 그 항목을 과하게 찾고 있다는 뜻입니다 (예: BIRTHDATE가 17이 아니라 50).
- **시간**: `deidentify()` 호출 시간의 합과 문서당 ms. 탐지기 생성 시간은 따로.
- **LLM 요청**: LLM 백엔드가 켜져 있으면 보낸 요청 수(실패 요청 수 포함). 규칙만이면 `-`.
- 처리 중 오류가 난 문서는 "실패"로 세고 점수에서 뺍니다. 연속 3개 문서가 실패하면(서버 꺼짐, 키 틀림)
  나머지는 돌리지 않고 멈춥니다. Ctrl-C로 멈춰도 그때까지의 결과를 보여 줍니다. 종료 코드는 항상 0입니다.
- `--verbose`는 문서별로 누출된 값과 과잉 변경 구간을, `--json`은 전체 결과를 파일로 남깁니다. **가상 코퍼스 전용**:
  실제 진료 기록을 넣으면 그 값이 화면과 파일에 그대로 나옵니다. 결과는 공개 이슈에 올리지 마세요.

---

## 3. 기준선: deid 규칙만 (기본 설정)

2026-10-17, deid 0.2.0 (커밋 1c5f025 + Strata 백엔드 작업 중인 소스), 기본 설정, 리눅스 개발 컨테이너.

```sh
python bench/score_values.py bench/fresh_a.jsonl bench/fresh_b.jsonl
```

| | 정답 값 | 누출 | 보호율 |
|---|---:|---:|---:|
| **전체** | **449** | **68** | **84.9%** |
| fresh_a | 221 | 20 | 91.0% |
| fresh_b (영문·외국인 많음) | 228 | 48 | 79.0% |
| 한국어 / 한영 혼용 / 영어 문서 | 279 / 117 / 53 | 30 / 26 / 12 | 89.2% / 77.8% / 77.4% |

| 항목 | 정답 | 누출 | 보호율 |
|---|---:|---:|---:|
| PERSON (이름) | 209 | 52 | 75.1% |
| MRN (등록번호) | 31 | 6 | 80.7% |
| PASSPORT | 10 | 2 | 80.0% |
| ADDRESS | 24 | 3 | 87.5% |
| MEDICAL_LICENSE | 10 | 1 | 90.0% |
| ACCOUNT | 11 | 1 | 90.9% |
| PHONE | 64 | 3 | 95.3% |
| BIRTHDATE, RRN, FRN, EMAIL, IP, PLATE, CARD, BRN, DRIVER_LICENSE, HEALTH_INSURANCE | 90 | 0 | 100% |

- keep 보존 **100% (457/457)**, 과잉 변경 **9곳** (0.09/문서), 0.24초 (2.5 ms/문서), LLM 요청 0.
- 누출 68개 중 52개가 이름입니다. 규칙이 놓치는 것: 영문·로마자·외국인 이름(직함 없이 쓴 `NGUYEN THI THU HA`,
  `PETROV IVAN`, `Olivia Bennett`), 한자 이름(`王丽华`, `张伟`)과 그 한글 표기, 단톡·메모 속 이름만 쓴 호칭(`수진`,
  `은지`, `민준이`, `준서`), 표·목록 속 라벨 없는 이름(`오정애`, `임소라`), 띄어 쓴 이름(`황보 영 진`, `남궁 율`).
  나머지는 라벨 없는 등록번호, 띄어 쓴 여권번호, 해외 전화번호, 시·구 없이 쓴 주소 등입니다.
- 과잉 변경 9곳: 방문 목록의 `어르신` 4곳이 이름으로 바뀜, `엄지랑 검지`의 `엄지랑`, 표 머리글 `사유`, `이름 미기재`,
  `공무팀`, 사고 접수번호.

Strata 패스가 도움이 된다면 무엇보다 **PERSON 75.1%와 전체 84.9%가 올라가야** 합니다.

---

## 4. 맥에서 Strata로 재기

진료실 PC(CLINIC1)의 Strata 서버에 같은 병원 네트워크의 맥에서 접속해 잽니다. 이 코퍼스는 가상 데이터라 원문이
PC로 전송되어도 괜찮습니다. (실제 기록을 `[ner] backend = "openai"`로 처리하면 비식별화 **전** 원문이 암호화되지
않은 http로 PC까지 갑니다. "이 맥 안에서만 처리"가 아니라 "진료실 PC까지"가 됩니다.)

**① 소스 폴더와 deid 버전 맞추기** — `bench/` 파일은 저장소 폴더에 있고, 실행하는 deid에는 openai 백엔드가 있어야 합니다.

```sh
cd ~/softkorea.github.io && git pull
pipx install --force --python python3.12 ~/softkorea.github.io/tools/deid
cd ~/softkorea.github.io/tools/deid
DEID_PY="$(pipx environment --value PIPX_LOCAL_VENVS)/deid-kit/bin/python"   # deid가 설치된 파이썬
"$DEID_PY" -c "import deid.backends.openai_compat; print('ok')"
```

(uv로 설치했다면 `DEID_PY="$(uv tool dir)/deid-kit/bin/python"`, 개발용 venv라면 `DEID_PY=.venv/bin/python`.)

**② API 키를 키체인에 넣기** — 키는 설정 파일·저장소·명령줄에 적지 않습니다. 아래처럼 `-w`를 맨 끝에 두면
키를 물어보니 거기에 붙여넣습니다(화면에 보이지 않고, 터미널 기록에도 남지 않습니다).

```sh
security add-generic-password -a deid -s deid-strata -U -w
```

처음 실행할 때 macOS가 키체인 접근을 물으면 "허용"을 누릅니다. (키체인 대신 `api_key_env = "STRATA_API_KEY"`로
환경변수를 쓸 수도 있습니다.)

**③ 벤치마크용 설정 파일** — 평소 쓰는 `~/.config/deid/config.toml`과 따로, 예를 들어 `~/deid-bench-strata.toml`:

```toml
[ner]
backend = "openai"
base_url = "http://192.168.0.20:8080/v1"   # 진료실 PC의 IP 주소 (http는 CLINIC1 같은 이름은 안 됨)
api_key_keychain = "deid-strata"      # ②에서 만든 키체인 항목 이름 (키 값이 아님)
model = "strata"
effort = "none"                       # 탐지에는 none이 가장 빠르고 medium과 놓치는 것이 같았음
allow_remote = true                   # 이 맥이 아닌 서버로 보내려면 꼭 필요
on_error = "fail"                     # 벤치마크에서는 실패를 숨기지 않도록 fail 권장
```

**④ 실행** — 먼저 3문서로 연결을 확인한 뒤 전체를 돌립니다.

```sh
"$DEID_PY" bench/score_values.py bench/fresh_a.jsonl --limit 3 --config ~/deid-bench-strata.toml
"$DEID_PY" bench/score_values.py bench/fresh_a.jsonl bench/fresh_b.jsonl \
    --config ~/deid-bench-strata.toml --json ~/Desktop/strata-bench.json --verbose
"$DEID_PY" bench/score_values.py bench/fresh_a.jsonl bench/fresh_b.jsonl     # 같은 맥에서 규칙만 (비교용)
```

- 문서 하나에 요청이 보통 1번이라 95번 정도 보냅니다. Strata는 한 번에 한 요청만 처리하고 Open WebUI·Claude Code와
  같이 쓰므로, PC가 한가할 때 돌리세요. 다른 작업이 겹치면 시간이 길어지고 `timeout`(기본 180초)에 걸릴 수 있습니다.
- `LLM 요청 requests` 줄이 `-`이면 LLM이 꺼져 있는 것입니다(설정 파일 경로 확인). `실패 failed`가 있으면 그 문서는
  점수에서 빠졌으니 `--verbose`의 실패 사유(401 키, 400 effort, 502 JSON 실패, 연결 안 됨)를 보고 다시 돌리세요.
- effort를 `low`/`medium`으로 바꿔 한 번씩 더 재면 속도와 보호율의 맞바꿈을 볼 수 있습니다.

---

## 5. 결과 읽는 법

규칙만(84.9%) 결과와 Strata 결과를 나란히 놓고 봅니다.

| 바뀐 것 | 뜻 |
|---|---|
| **전체·PERSON 보호율 ↑** | LLM이 규칙이 놓친 값을 잡았다. 2절의 누출 52개 이름 중 몇 개가 사라졌는지가 핵심. |
| **과잉 변경 ↑, keep 보존 ↓** | 과잉 마스킹. 개인정보가 아닌 진료 날짜·약·수치를 가렸다. PC에서 deid 자체 코퍼스로 쟀을 때도 진료 날짜 36개 중 7개가 가려졌습니다. `탐지 spans`의 BIRTHDATE가 정답(17)보다 많은지, `--verbose`의 `과잉 spurious` 줄에 날짜가 있는지 보세요. |
| **ms/문서 ↑** | LLM 왕복 시간. 실제로 쓸 때의 대기 시간입니다. |
| `ORG` 탐지 spans | LLM이 찾은 병원·회사 이름. 기본 동작이 `keep`(그대로 둠)이라 과잉 변경에 잡히지 않습니다. |

- 판단 기준(제안): 보호율이 뚜렷이 오르고(특히 PERSON), 과잉 변경과 keep 손상이 조금만 늘었다면 켤 가치가 있습니다.
  누출이 줄어도 진료 날짜가 계속 가려진다면 프롬프트나 날짜 처리부터 손봐야 합니다.
- 같은 설정이라도 서버 캐시 때문에 결과가 조금 달라질 수 있습니다. 차이가 작으면 두 번 재서 보세요.
- `on_error = "rules_only"`로 재면 실패한 요청이 있는 문서는 규칙만으로 채점되고 `LLM 요청 … 실패 failed n`으로만
  드러나므로, 비교에는 `fail`이 낫습니다.

---

## 6. 주의: 한 번 맞추면 더는 held-out이 아니다

이 두 코퍼스로 **프롬프트나 규칙을 고치기 시작하면, 그 순간부터 이 코퍼스의 숫자는 낙관적인 값**이 됩니다
(deid 자체 코퍼스가 99%인 것과 같은 이유). 그래서:

- 고치기 전 숫자(규칙만, Strata 첫 측정)를 먼저 기록해 두세요.
- `--verbose`로 놓친 값을 보고 프롬프트를 고쳤다면, 그 뒤의 점수는 "튜닝 후(이 코퍼스 기준)"라고 적어 두세요.
- 다음 판단에는 프롬프트와 deid 규칙을 보지 않은 사람(또는 다른 에이전트)이 새로 쓴 코퍼스가 필요합니다.
  같은 형식(`id, genre, lang, text, pii, keep`)으로 만들면 이 스크립트를 그대로 쓸 수 있습니다.
