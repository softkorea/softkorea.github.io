# SEO 파일 업데이트 (robots.txt, llms.txt)

포스트가 추가되거나 변경된 후 `robots.txt`와 `llms.txt`를 최신 상태로 재생성한다.

## 실행 절차

### 1단계: 현재 포스트 목록 수집
- `_posts/` 디렉토리의 모든 `.md` 파일을 읽는다.
- 각 포스트의 front matter에서 `title`, `date`, `categories`, `tags`, `description`을 추출한다.
- 날짜 기준 최신순으로 정렬한다.

### 2단계: llms.txt 재생성
- 아래 구조로 `llms.txt`를 재생성한다:
  - Jekyll front matter (`layout: none`) 포함
  - 사이트 소개 (title, tagline, 저자 정보)
  - 카테고리별 설명
  - **전체 포스트 목록**: 카테고리별로 그룹핑하여 제목, 날짜, description 포함
- `_config.yml`에서 `title`, `tagline`, `description`, `url` 값을 읽어서 반영한다.
- 포스트 URL 형식: `{{ site.url }}/posts/슬러그/`

### 3단계: robots.txt 확인
- `robots.txt`가 존재하는지 확인한다.
- 존재하면 기존 내용을 유지한다 (AI 크롤러 허용 규칙은 정적이므로 변경 불필요).
- 존재하지 않으면 아래 내용으로 새로 생성한다:
  ```
  ---
  layout: none
  ---
  User-agent: *
  Allow: /
  Sitemap: {{ site.url }}/sitemap.xml

  User-agent: GPTBot
  Allow: /

  User-agent: OAI-SearchBot
  Allow: /

  User-agent: ChatGPT-User
  Allow: /

  User-agent: ClaudeBot
  Allow: /

  User-agent: anthropic-ai
  Allow: /

  User-agent: claude-web
  Allow: /

  User-agent: Claude-SearchBot
  Allow: /

  User-agent: Google-Extended
  Allow: /

  User-agent: Applebot-Extended
  Allow: /

  User-agent: PerplexityBot
  Allow: /

  User-agent: Meta-ExternalAgent
  Allow: /

  User-agent: Amazonbot
  Allow: /

  User-agent: CCBot
  Allow: /

  User-agent: YouBot
  Allow: /

  User-agent: PhindBot
  Allow: /

  User-agent: cohere-ai
  Allow: /

  User-agent: Bytespider
  Allow: /
  ```

### 4단계: 결과 보고
- 업데이트된 llms.txt의 포스트 수를 보고한다.
- 새로 추가된 포스트가 있으면 해당 항목을 하이라이트한다.
