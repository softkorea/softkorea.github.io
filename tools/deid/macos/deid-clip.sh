#!/bin/sh
# deid clip: 클립보드의 텍스트(또는 이미지)를 비식별화해 다시 클립보드에 넣습니다.
# 단축어(Shortcuts) 앱의 '셸 스크립트 실행' 동작이나 키보드 단축키에서 부르기 위한 래퍼입니다.
# 단축어/Automator는 터미널과 달리 PATH와 언어 설정이 비어 있어서 여기서 채워 줍니다.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export LANG="en_US.UTF-8" LC_ALL="en_US.UTF-8"

if ! command -v deid >/dev/null 2>&1; then
  osascript -e 'display notification "deid 명령을 찾을 수 없습니다. 설치를 확인하세요 (deid doctor)." with title "deid"'
  exit 127
fi
# --notify: 끝나면 알림(개수만 표시, 원문은 표시하지 않음). 실패하면 클립보드를 비웁니다.
exec deid clip --notify "$@"
