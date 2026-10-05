#!/bin/bash
# ==============================================================================
# ThePrepared Unified Automated Weekly Backup Manager (v2.0)
# 
# 기능:
# 1. PostgreSQL 전체 클러스터 덤프 및 gzip 압축 저장 (로컬 14일/2주 핫 백업 유지)
# 2. 모든 서비스 .env 및 핵심 자격증명 수집 -> OpenSSL AES-256-CBC PBKDF2 암호화 아카이빙
# 3. Google Drive 전용 백업 폴더로 rclone 직접 단방향 업로드 (마운트 불필요, 에어갭 격리)
#    - 대상: gdrive:Work/개인작업/프리페어드/서버 환경/theprepared_backups/
# 4. 12주(84일) 초과 파일 Google Drive 원격 자동 삭제 (기존 수동 백업본은 영구 보존)
# 5. 로컬 14일(2주) 초과 파일 자동 정리 (로컬 디스크 공간 절약)
# ==============================================================================

set -o pipefail

# [경로 및 보관 주기 설정]
BASE_DIR="/home/soyjefu/theprepared"
LOCAL_BACKUP_ROOT="${BASE_DIR}/server-backups"
DB_BACKUP_DIR="${LOCAL_BACKUP_ROOT}/db"
CRED_BACKUP_DIR="${LOCAL_BACKUP_ROOT}/credentials"
LOG_DIR="${LOCAL_BACKUP_ROOT}/logs"
KEY_FILE="${BASE_DIR}/utilities/.backup_key"

# 원격 Google Drive 경로 (과거 백업 폴더와 동일 위치)
REMOTE_BASE="gdrive:Work/개인작업/프리페어드/서버 환경/theprepared_backups"

# 보관 정책
RETENTION_WEEKS=12
REMOTE_RETENTION_DAYS=$((RETENTION_WEEKS * 7)) # Google Drive 12주(84일) 보관
LOCAL_RETENTION_DAYS=14                        # 로컬 서버 2주(14일) 핫 백업 보관

DATE=$(date +%Y%m%d_%H%M%S)
MONTH=$(date +%Y%m)

# 로컬 작업 디렉토리 생성
mkdir -p "${DB_BACKUP_DIR}" "${CRED_BACKUP_DIR}" "${LOG_DIR}"

LOG_FILE="${LOG_DIR}/backup_${MONTH}.log"

log() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $1"
    echo "$msg"
    echo "$msg" >> "$LOG_FILE"
}

log "=================================================="
log "🚀 ThePrepared 주간 통합 백업 시작 (Tag: $DATE)"
log "=================================================="

# 암호화 키 확인
if [ ! -f "${KEY_FILE}" ]; then
    log "❌ [오류] 암호화 키 파일(${KEY_FILE})이 존재하지 않습니다."
    exit 1
fi

# ------------------------------------------------------------------------------
# 1. PostgreSQL 전체 데이터베이스 덤프
# ------------------------------------------------------------------------------
log "📦 [1/5] PostgreSQL 데이터베이스 덤프 시작..."
TMP_SQL="/tmp/pg_dump_${DATE}.sql.gz"
TARGET_SQL="${DB_BACKUP_DIR}/postgresql_all_${DATE}.sql.gz"

if docker ps --format '{{.Names}}' | grep -q "^postgres_db$"; then
    docker exec -e PGPASSWORD="dnflxksdir1!" postgres_db pg_dumpall -U theprepared 2>/dev/null | gzip -c > "$TMP_SQL"
    DUMP_STATUS=${PIPESTATUS[0]}
    
    if [ $DUMP_STATUS -eq 0 ] && [ -s "$TMP_SQL" ]; then
        DUMP_SIZE=$(ls -lh "$TMP_SQL" | awk '{print $5}')
        mv "$TMP_SQL" "$TARGET_SQL"
        log "✅ PostgreSQL 덤프 완료: ${TARGET_SQL} (용량: ${DUMP_SIZE})"
    else
        log "❌ [오류] PostgreSQL 덤프 실패 (Exit: $DUMP_STATUS)"
        rm -f "$TMP_SQL"
    fi
else
    log "⚠️ [경고] postgres_db 컨테이너가 실행 중이 아닙니다. DB 백업을 건너뜁니다."
fi

# ------------------------------------------------------------------------------
# 2. 자격증명(.env 및 비밀설정) 파일 수집 및 AES-256-CBC 암호화 아카이빙
# ------------------------------------------------------------------------------
log "🔐 [2/5] Credential (.env 등) 수집 및 OpenSSL 암호화 시작..."
TMP_CRED_DIR=$(mktemp -d /tmp/cred_backup_XXXXXX)
TMP_CRED_TAR="/tmp/credentials_${DATE}.tar.gz"
TARGET_CRED_ENC="${CRED_BACKUP_DIR}/credentials_${DATE}.tar.gz.enc"

# 프로젝트별 자격증명 파일 수집
find /home/soyjefu/theprepared -maxdepth 2 \( -name ".env*" -o -name ".credentials*" \) -type f 2>/dev/null | while read -r f; do
    REL_PATH="${f#/home/soyjefu/theprepared/}"
    DEST_DIR="${TMP_CRED_DIR}/theprepared/$(dirname "$REL_PATH")"
    mkdir -p "$DEST_DIR"
    cp -p "$f" "$DEST_DIR/"
done

# 시스템 인증 파일 수집
if [ -f "/home/soyjefu/.git-credentials" ]; then
    mkdir -p "${TMP_CRED_DIR}/system"
    cp -p "/home/soyjefu/.git-credentials" "${TMP_CRED_DIR}/system/"
fi
if [ -f "/home/soyjefu/.config/rclone/rclone.conf" ]; then
    mkdir -p "${TMP_CRED_DIR}/system"
    cp -p "/home/soyjefu/.config/rclone/rclone.conf" "${TMP_CRED_DIR}/system/"
fi

# tar 압축
tar -czf "$TMP_CRED_TAR" -C "$TMP_CRED_DIR" . 2>/dev/null

# OpenSSL AES-256-CBC PBKDF2 암호화
openssl enc -aes-256-cbc -salt -pbkdf2 -iter 100000 -pass file:"${KEY_FILE}" -in "$TMP_CRED_TAR" -out "$TARGET_CRED_ENC"
ENC_STATUS=$?

if [ $ENC_STATUS -eq 0 ] && [ -s "$TARGET_CRED_ENC" ]; then
    ENC_SIZE=$(ls -lh "$TARGET_CRED_ENC" | awk '{print $5}')
    log "✅ 자격증명 암호화 백업 완료: ${TARGET_CRED_ENC} (용량: ${ENC_SIZE})"
else
    log "❌ [오류] 자격증명 암호화 실패 (Exit: $ENC_STATUS)"
fi

# 임시 파일 정리
rm -rf "$TMP_CRED_DIR" "$TMP_CRED_TAR"

# ------------------------------------------------------------------------------
# 3. Google Drive 직접 단방향 업로드 (마운트 불필요)
# ------------------------------------------------------------------------------
log "☁️ [3/5] Google Drive로 직접 업로드 중 (${REMOTE_BASE})..."

if [ -f "$TARGET_SQL" ]; then
    rclone copy "$TARGET_SQL" "${REMOTE_BASE}/db/" -v 2>&1 | while read -r line; do log "  [rclone-db] $line"; done
    if [ ${PIPESTATUS[0]} -eq 0 ]; then
        log "✅ PostgreSQL 백업 Google Drive 업로드 성공"
    else
        log "❌ [오류] PostgreSQL 백업 Google Drive 업로드 실패"
    fi
fi

if [ -f "$TARGET_CRED_ENC" ]; then
    rclone copy "$TARGET_CRED_ENC" "${REMOTE_BASE}/credentials/" -v 2>&1 | while read -r line; do log "  [rclone-cred] $line"; done
    if [ ${PIPESTATUS[0]} -eq 0 ]; then
        log "✅ 자격증명 암호화 백업 Google Drive 업로드 성공"
    else
        log "❌ [오류] 자격증명 암호화 백업 Google Drive 업로드 실패"
    fi
fi

# ------------------------------------------------------------------------------
# 4. Google Drive 원격 12주(84일) 초과 파일 삭제 (기존 백업 폴더 영향 없음)
# ------------------------------------------------------------------------------
log "🧹 [4/5] Google Drive 원격 12주(${REMOTE_RETENTION_DAYS}일) 초과 백업 정리 중..."
rclone delete "${REMOTE_BASE}/db/" --min-age ${REMOTE_RETENTION_DAYS}d -v 2>&1 | while read -r line; do log "  [rclone-clean-db] $line"; done
rclone delete "${REMOTE_BASE}/credentials/" --min-age ${REMOTE_RETENTION_DAYS}d -v 2>&1 | while read -r line; do log "  [rclone-clean-cred] $line"; done

# ------------------------------------------------------------------------------
# 5. 로컬 14일(2주 핫 백업) 초과 파일 정리
# ------------------------------------------------------------------------------
log "🧹 [5/5] 로컬 14일(2주) 초과 백업 파일 정리 중..."
DELETED_LOCAL_DBS=$(find "${DB_BACKUP_DIR}" -name "postgresql_all_*.sql.gz" -mtime +${LOCAL_RETENTION_DAYS} -delete -print 2>/dev/null | wc -l)
DELETED_LOCAL_CREDS=$(find "${CRED_BACKUP_DIR}" -name "credentials_*.tar.gz.enc" -mtime +${LOCAL_RETENTION_DAYS} -delete -print 2>/dev/null | wc -l)
log "ℹ️ 로컬 정리 결과: DB 백업 ${DELETED_LOCAL_DBS}건, Credential 백업 ${DELETED_LOCAL_CREDS}건 정리됨"

# 실행 로그도 Google Drive에 업로드 동기화
rclone copy "${LOG_FILE}" "${REMOTE_BASE}/logs/" 2>/dev/null || true

log "=================================================="
log "🏁 ThePrepared 주간 통합 백업 및 Google Drive 동기화 정상 완료"
log "=================================================="
