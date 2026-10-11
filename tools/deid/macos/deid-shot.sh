#!/bin/sh
# deid shot: 화면 일부를 선택해 캡처 -> 개인정보 가림 -> ~/Deid/out 저장 + 클립보드에 복사.
# 창 하나: deid-shot.sh --window   전체 화면: deid-shot.sh --full   (Esc로 취소)
# 단축어(Shortcuts) 앱이나 키보드 단축키에서 부르기 위한 래퍼입니다.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export LANG="en_US.UTF-8" LC_ALL="en_US.UTF-8"

if ! command -v deid >/dev/null 2>&1; then
  osascript -e 'display notification "deid 명령을 찾을 수 없습니다. 설치를 확인하세요 (deid doctor)." with title "deid"'
  exit 127
fi
exec deid shot --notify "$@"
