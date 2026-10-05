#!/bin/bash
# ==============================================================================
# ThePrepared Credential One-Click Restore Script (v1.0)
# 
# 기능:
# 1. 가장 최신의 credentials_*.tar.gz.enc 파일을 자동으로 찾아 복호화
# 2. 복구 대상 디렉토리에 .env 및 설정 파일들을 구조 그대로 복원
# 3. 사용법:
#    - 기본 복구 (임시 복구 폴더로 추출):
#      ./restore_credentials.sh
#    - 특정 폴더로 복구:
#      ./restore_credentials.sh /복구/원하는/경로
#    - 특정 암호화 파일 지정 복구:
#      ./restore_credentials.sh /복구/원하는/경로 /경로/credentials_XXXX.tar.gz.enc
# ==============================================================================

set -eo pipefail

BASE_DIR="/home/soyjefu/theprepared"
BACKUP_DIR="${BASE_DIR}/server-backups/credentials"
KEY_FILE="${BASE_DIR}/utilities/.backup_key"

TARGET_DIR="${1:-${BASE_DIR}/restored_credentials_$(date +%Y%m%d_%H%M%S)}"
SPECIFIED_ENC_FILE="$2"

echo "=================================================="
echo "🔓 ThePrepared 자격증명(Credential) 복구 도구"
echo "=================================================="

# 1. 암호화 파일 결정
if [ -n "$SPECIFIED_ENC_FILE" ]; then
    ENC_FILE="$SPECIFIED_ENC_FILE"
else
    ENC_FILE=$(ls -t "${BACKUP_DIR}"/credentials_*.tar.gz.enc 2>/dev/null | head -n 1)
fi

if [ -z "$ENC_FILE" ] || [ ! -f "$ENC_FILE" ]; then
    echo "❌ [오류] 복구할 암호화 백업 파일(.tar.gz.enc)을 찾을 수 없습니다: ${BACKUP_DIR}"
    exit 1
fi

# 2. 마스터 키 확인
if [ ! -f "$KEY_FILE" ]; then
    echo "⚠️ [알림] 서버 내 키 파일(${KEY_FILE})이 없습니다."
    read -rsp "🔑 마스터 암호화 키를 직접 입력하세요: " MANUAL_KEY
    echo ""
    PASS_OPT=(-pass "pass:$MANUAL_KEY")
else
    PASS_OPT=(-pass "file:$KEY_FILE")
fi

echo "📦 대상 백업 파일: $(basename "$ENC_FILE")"
echo "📂 복원 대상 경로: $TARGET_DIR"

# 3. 디렉토리 생성 및 복호화 추출
mkdir -p "$TARGET_DIR"

openssl enc -d -aes-256-cbc -pbkdf2 -iter 100000 \
    "${PASS_OPT[@]}" \
    -in "$ENC_FILE" \
    | tar -xzf - -C "$TARGET_DIR"

if [ $PIPESTATUS -eq 0 ]; then
    echo "✅ 복호화 및 파일 복원이 성공적으로 완료되었습니다!"
    echo "--------------------------------------------------"
    echo "📋 복원된 파일 목록:"
    find "$TARGET_DIR" -type f | sed "s|^$TARGET_DIR/|  - |"
    echo "--------------------------------------------------"
    echo "💡 복원된 파일 확인 경로: $TARGET_DIR"
else
    echo "❌ [오류] 복호화에 실패했습니다. 암호화 키 또는 파일 손상 여부를 확인하세요."
    exit 1
fi

echo "=================================================="
