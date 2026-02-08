# 드래프트 포스트 발행

`.claude-workspace/drafts/` 에 있는 드래프트 포스트를 `_posts/`로 발행하는 워크플로우를 실행한다.

인자: $ARGUMENTS (드래프트 파일명 키워드 또는 날짜, 예: "1-31", "code-explosion")

## 실행 절차

### 1단계: 드래프트 파일 찾기
- `.claude-workspace/drafts/` 디렉토리에서 `$ARGUMENTS` 키워드에 매칭되는 `.md` 파일을 검색한다.
- 매칭되는 파일이 여러 개이면 사용자에게 선택을 요청한다.
- 매칭되는 파일이 없으면 전체 드래프트 목록을 보여주고 사용자에게 선택을 요청한다.

### 2단계: 드래프트 내용 확인
- 드래프트 파일을 읽고 front matter를 분석한다.
- 파일명에서 날짜(YYYY-MM-DD)와 슬러그를 추출한다.

### 3단계: Front Matter 업데이트
기존 포스트 컨벤션에 맞춰 front matter를 정리한다:
- `date`: `YYYY-MM-DD HH:MM:SS +0900` 형식 확인 (시간이 없으면 `12:00:00 +0900` 추가)
- `author`: `dr_softkorea` 확인 (없으면 추가)
- `categories`: 기존 카테고리 체계(`Diary`, `DocSkills`, `CodeLog`) 확인

### 4단계: 커버 이미지 처리
- `.claude-workspace/drafts/images/` 에서 커버 이미지 후보를 찾는다 (cover, 또는 포스트명 관련 이미지).
- 커버 이미지가 있으면:
  1. `assets/img/posts/YYYY-MM-DD-슬러그/` 디렉토리 생성
  2. 이미지 정보 확인 (`magick identify`)
  3. **WebP 변환**: `magick input -quality 85 output.webp` (PNG/JPG/JIFF 등 → WebP)
  4. **리사이즈**: 가로가 1200px 초과이면 1200px로 리사이즈, 이하이면 원본 크기 유지
  5. `cover.webp`로 저장
  6. 원본 파일 삭제하지 않음 (사용자가 직접 정리)
  7. front matter에 `image:` 섹션 추가:
     ```yaml
     image:
       path: /assets/img/posts/YYYY-MM-DD-슬러그/cover.webp
       alt: "이미지 설명"
     ```
  8. alt 텍스트는 이미지 내용을 보고 적절하게 작성
- 커버 이미지가 없으면 사용자에게 확인 후 이미지 없이 진행한다.

### 5단계: 본문 이미지 처리
- 드래프트 본문에서 `images/` 경로를 참조하는 이미지를 찾는다.
- 해당 이미지들도 동일하게:
  1. WebP로 변환 (이미 WebP이면 스킵)
  2. `assets/img/posts/YYYY-MM-DD-슬러그/`로 복사
  3. 본문의 이미지 경로를 `/assets/img/posts/YYYY-MM-DD-슬러그/파일명.webp`로 변경

### 6단계: 포스트 파일 생성
- 최종 내용을 `_posts/YYYY-MM-DD-슬러그.md`로 저장한다.

### 7단계: 결과 보고
- 변환 결과를 요약 테이블로 보여준다:
  - 파일명, 이미지 변환 결과 (원본 크기 → 변환 후 크기), front matter 변경 사항
- 드래프트 원본 파일은 삭제하지 않는다 (사용자가 확인 후 직접 정리).
- **발행 완료 후 `/update-seo` 실행을 안내한다** (llms.txt에 새 포스트 반영).
