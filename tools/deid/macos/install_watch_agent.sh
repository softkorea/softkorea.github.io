#!/bin/sh
# ~/Deid/in 폴더에 넣은 파일을 ~/Deid/out 으로 자동 비식별화하는 백그라운드 작업(launchd)을
# 설치합니다. 로그인할 때마다 자동으로 시작됩니다.
#
#   sh install_watch_agent.sh               설치 (다시 실행하면 새 설정으로 재설치)
#   sh install_watch_agent.sh --uninstall   제거
#   sh install_watch_agent.sh --print       설치하지 않고 설정(plist)만 보기
#
# 실제 작업은 'deid install-watch'가 합니다: ~/Library/LaunchAgents/com.softkorea.deid.watch.plist
# 를 만들고 'launchctl bootstrap gui/$(id -u)'로 등록합니다. 기록: ~/Library/Logs/deid-watch.log
set -eu
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export LANG="en_US.UTF-8" LC_ALL="en_US.UTF-8"

if ! command -v deid >/dev/null 2>&1; then
  echo "deid 명령을 찾을 수 없습니다. 먼저 deid를 설치하세요 (README.md 참고)." >&2
  exit 127
fi
exec deid install-watch "$@"
