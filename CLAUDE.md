# CLAUDE.md

이 파일은 Claude Code가 이 프로젝트 작업 시 참고하는 컨텍스트 파일입니다.

## 프로젝트 개요

- **타입**: Jekyll 기반 GitHub Pages 블로그
- **테마**: jekyll-theme-chirpy (v7.4+)
- **언어**: 한국어 (ko-KR)
- **URL**: https://softkorea.github.io

## 주요 디렉토리

- `_posts/` - 블로그 포스트 (YYYY-MM-DD-title.md 형식)
- `_tabs/` - 네비게이션 페이지 (about, archives, categories, tags)
- `_data/` - 데이터 파일 (authors.yml, contact.yml, share.yml)
- `_plugins/` - 커스텀 Jekyll 플러그인
- `assets/lib/` - Chirpy 테마 에셋 (Git submodule)

## 자주 사용하는 명령어

```bash
# 로컬 개발 서버 실행
bundle exec jekyll serve --livereload

# 또는 스크립트 사용
bash tools/run.sh

# 빌드 테스트
bash tools/test.sh

# 의존성 설치
bundle install
```

## 포스트 작성 규칙

### 파일명
`_posts/YYYY-MM-DD-제목.md`

### Front Matter 템플릿
```yaml
---
title: "포스트 제목"
date: YYYY-MM-DD HH:MM:SS +0900
categories: [카테고리]
tags: [태그1, 태그2]
author: dr_softkorea
---
```

### 선택적 Front Matter
```yaml
mermaid: true      # Mermaid 다이어그램 사용 시
math: true         # 수학 수식 사용 시
pin: true          # 포스트 상단 고정
image:             # 썸네일 이미지
  path: /path/to/image.jpg
  alt: 이미지 설명
```

## 저자 정보

`_data/authors.yml`에 정의됨:
- `dr_softkorea`: Dr.softkorea (기본 저자)

## 카테고리/태그

현재 사용 중인 카테고리:
- `diary` - 일기/개인 기록
- `DocSkills` - 진료실 실용 기술

## 배포

- main 브랜치에 push하면 GitHub Actions가 자동 빌드/배포
- 워크플로우: `.github/workflows/pages-deploy.yml`

## 코드 스타일

- **들여쓰기**: 2 spaces
- **문자셋**: UTF-8
- **줄바꿈**: LF (Unix)
- **YAML**: 쌍따옴표 사용

## 이미지 워크플로우

**상세 가이드**: `.claude-workspace/IMAGE-WORKFLOW.md`

### 요약
1. **초안 작성 시**: `.claude-workspace/drafts/images/`에 임시 저장
2. **완성 후**: `assets/img/posts/YYYY-MM-DD-제목/`으로 이동
3. **참조 방식**: `/assets/img/posts/YYYY-MM-DD-제목/파일명.png`

### 커버 이미지 설정
```yaml
image:
  path: /assets/img/posts/YYYY-MM-DD-제목/cover.jpg
  alt: "이미지 설명"
```

## 크로스 플랫폼 (Windows / Mac)

- Synology Drive를 통해 Windows와 Mac에서 공유
- `.gitattributes`에서 `* text=auto eol=lf`로 줄바꿈 통일 (LF)
- Ruby/Jekyll 환경은 각 OS에서 `bundle install`로 별도 설치
- 파일명 대소문자를 일관되게 유지할 것 (Mac/Windows 모두 기본적으로 대소문자 구분 안 함)
- 동시에 양쪽에서 같은 파일 수정 금지 — Git을 통해 변경사항 관리

## 주의사항

- `assets/lib/`는 Git submodule이므로 직접 수정하지 않음
- Gemfile.lock은 .gitignore에 포함됨
- 이미지는 `assets/img/posts/` 폴더에 포스트별로 저장
