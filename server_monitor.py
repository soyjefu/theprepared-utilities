#!/usr/bin/env python3
"""
[The Prepared] 통합 서버 백그라운드 모니터링 시스템 (Server-Wide Health Monitor)
================================================================================
감시 대상:
  1. 가계부 (PFM / account_app): 컨테이너 상태, HTTP 200, PostgreSQL
  2. 투자 시스템 (Invest): 5대 컨테이너, Celery 워커/비트, 웹소켓 워치독, V2 헬스체크
  3. 블로그 (Django Blog): 3대 컨테이너, Port 8011 HTTP 200
  4. 오토포스트 (Autopost): 5대 컨테이너, Celery 워커
  5. 인프라: PostgreSQL (5432), Redis (6379), Nginx, Traefik
  6. 백업 시스템: 주간 PostgreSQL 및 암호화 Credential 백업 건전성
  7. 시스템 자원: 디스크 여유 공간 (<85%), 가용 메모리 (>2GB), CPU 부하
"""

import os
import sys
import json
import time
import shutil
import argparse
import subprocess
import urllib.request
import urllib.error
from datetime import datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
BASE_DIR = "/home/soyjefu/theprepared"
INVEST_DIR = os.path.join(BASE_DIR, "invest")
ENV_FILE = os.path.join(INVEST_DIR, ".env")


def load_env_var(key: str, default: str = "") -> str:
    """invest/.env 파일에서 특정 환경변수 로드"""
    if not os.path.exists(ENV_FILE):
        return default
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith(f"{key}="):
                val = line.split("=", 1)[1].strip()
                return val.strip("\"'")
    return default


def get_docker_containers() -> dict:
    """실행 중인 모든 도커 컨테이너 상태 맵 반환"""
    try:
        res = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Status}}\t{{.State}}"],
            capture_output=True,
            text=True,
            timeout=5
        )
        containers = {}
        for line in res.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 3:
                containers[parts[0]] = {
                    "status": parts[1],
                    "state": parts[2].lower()
                }
        return containers
    except Exception as e:
        return {"_error": str(e)}


def check_http(url: str, expected_codes=(200, 301, 302), headers=None, timeout=3) -> dict:
    """HTTP 엔드포인트 헬스체크"""
    req_headers = {"User-Agent": "ThePrepared-ServerMonitor/1.0"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    try:
        start = time.time()
        with urllib.request.urlopen(req, timeout=timeout) as response:
            latency_ms = round((time.time() - start) * 1000, 1)
            code = response.getcode()
            return {
                "ok": code in expected_codes,
                "code": code,
                "latency_ms": latency_ms
            }
    except urllib.error.HTTPError as e:
        return {
            "ok": e.code in expected_codes,
            "code": e.code,
            "latency_ms": 0.0,
            "msg": f"HTTP {e.code}"
        }
    except Exception as e:
        return {
            "ok": False,
            "code": 0,
            "latency_ms": 0.0,
            "msg": str(e)
        }


def check_system_resources() -> dict:
    """서버 리소스 (디스크, RAM, CPU 부하) 검사"""
    usage = shutil.disk_usage("/")
    disk_total_gb = round(usage.total / (1024 ** 3), 1)
    disk_used_gb = round(usage.used / (1024 ** 3), 1)
    disk_free_gb = round(usage.free / (1024 ** 3), 1)
    disk_pct = round((usage.used / usage.total) * 100, 1)

    mem_total_mb, mem_avail_mb = 0, 0
    try:
        with open("/proc/meminfo", "r") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    mem_total_mb = int(line.split()[1]) // 1024
                elif line.startswith("MemAvailable:"):
                    mem_avail_mb = int(line.split()[1]) // 1024
    except Exception:
        pass

    load_1, load_5, load_15 = os.getloadavg()
    cpu_cores = os.cpu_count() or 4

    return {
        "disk": {
            "total_gb": disk_total_gb,
            "used_gb": disk_used_gb,
            "free_gb": disk_free_gb,
            "used_pct": disk_pct,
            "ok": disk_pct < 85.0
        },
        "memory": {
            "total_mb": mem_total_mb,
            "avail_mb": mem_avail_mb,
            "avail_gb": round(mem_avail_mb / 1024, 1),
            "ok": mem_avail_mb >= 2048
        },
        "cpu": {
            "load_1m": round(load_1, 2),
            "load_5m": round(load_5, 2),
            "load_15m": round(load_15, 2),
            "cores": cpu_cores,
            "ok": (load_15 / cpu_cores) < 2.0
        }
    }


def check_backup_status() -> dict:
    """정기 백업 상태 점검 (최근 8일 이내 정상 백업 존재 여부)"""
    backup_db_dir = os.path.join(BASE_DIR, "server-backups", "db")
    backup_cred_dir = os.path.join(BASE_DIR, "server-backups", "credentials")

    now = time.time()
    max_age_sec = 8 * 86400  # 8일 (주간 백업 + 1일 버퍼)

    latest_db_file = None
    latest_db_mtime = 0
    if os.path.exists(backup_db_dir):
        for fname in os.listdir(backup_db_dir):
            if fname.startswith("postgresql_all_") and fname.endswith(".sql.gz"):
                fpath = os.path.join(backup_db_dir, fname)
                mtime = os.path.getmtime(fpath)
                if mtime > latest_db_mtime:
                    latest_db_mtime = mtime
                    latest_db_file = fpath

    latest_cred_file = None
    latest_cred_mtime = 0
    if os.path.exists(backup_cred_dir):
        for fname in os.listdir(backup_cred_dir):
            if fname.startswith("credentials_") and fname.endswith(".tar.gz.enc"):
                fpath = os.path.join(backup_cred_dir, fname)
                mtime = os.path.getmtime(fpath)
                if mtime > latest_cred_mtime:
                    latest_cred_mtime = mtime
                    latest_cred_file = fpath

    if not latest_db_file or not latest_cred_file:
        return {
            "ok": False,
            "summary": "백업 파일 부재 (DB 또는 Credential 파일 없음)",
            "issue": "정기 백업 파일이 존재하지 않습니다."
        }

    db_age = now - latest_db_mtime
    cred_age = now - latest_cred_mtime

    if db_age > max_age_sec or cred_age > max_age_sec:
        delay_days = round(max(db_age, cred_age) / 86400, 1)
        return {
            "ok": False,
            "summary": f"백업 지연 감지 ({delay_days}일 전 백업이 마지막)",
            "issue": f"정기 백업이 8일 이상 지연되었습니다 (마지막 백업: {delay_days}일 전)."
        }

    latest_dt = datetime.fromtimestamp(latest_db_mtime, KST).strftime("%Y-%m-%d %H:%M")
    db_size_mb = round(os.path.getsize(latest_db_file) / (1024 * 1024), 1)

    return {
        "ok": True,
        "summary": f"최신 백업 정상 ({latest_dt} | DB {db_size_mb}MB)",
        "issue": None
    }


def send_discord_alert(report: dict, webhook_url: str):
    """디스코드 웹훅으로 상태 보고 전송"""
    if not webhook_url:
        return

    now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")
    overall_status = report["overall"]
    
    color_map = {
        "HEALTHY": 0x2ECC71,  # Green
        "WARNING": 0xF1C40F,  # Yellow
        "CRITICAL": 0xE74C3C  # Red
    }
    color = color_map.get(overall_status, 0x95A5A6)

    desc_lines = [
        f"**점검 일시:** `{now_str}`",
        f"**종합 판정:** `{overall_status}`\n"
    ]

    for domain, data in report["domains"].items():
        icon = "✅" if data["ok"] else "❌"
        desc_lines.append(f"{icon} **{domain}**: {data['summary']}")

    res_data = report["resources"]
    desc_lines.append(
        f"\n📊 **서버 자원**: 디스크 {res_data['disk']['used_pct']}% 사용 (여유 {res_data['disk']['free_gb']}GB) | "
        f"RAM 가용 {res_data['memory']['avail_gb']}GB | CPU 부하 {res_data['cpu']['load_5m']}"
    )

    payload = {
        "embeds": [{
            "title": f"🖥️ [The Prepared] 서버 전체 통합 모니터링: {overall_status}",
            "description": "\n".join(desc_lines),
            "color": color,
            "footer": {
                "text": "The Prepared Server Watchdog (v134.2)"
            }
        }]
    }

    try:
        req = urllib.request.Request(
            webhook_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "ThePrepared-Monitor/1.0"}
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"Failed to send Discord alert: {e}", file=sys.stderr)


def run_full_server_inspection() -> dict:
    """전체 5대 도메인 및 서버 자원 전수 점검"""
    now_kst = datetime.now(KST)
    containers = get_docker_containers()
    resources = check_system_resources()

    domains = {}
    issues = []

    # 1. 투자 시스템 (Invest)
    invest_containers = [
        "invest_app", "invest_celery_worker_fast", "invest_celery_worker_heavy",
        "invest_celery_beat", "invest_websocket_monitor"
    ]
    invest_up = [c for c in invest_containers if containers.get(c, {}).get("state") == "running"]
    invest_http = check_http("http://127.0.0.1:8001/portfolio/", expected_codes=(200, 302))

    invest_ok = (len(invest_up) == len(invest_containers)) and invest_http["ok"]
    if not invest_ok:
        issues.append(f"Invest 컨테이너/HTTP 결함 ({len(invest_up)}/{len(invest_containers)} Up)")

    domains["1. 투자 시스템 (Invest)"] = {
        "ok": invest_ok,
        "summary": f"5대 컨테이너 {len(invest_up)}/5 Up | HTTP 8001 {invest_http['code']} ({invest_http['latency_ms']}ms)"
    }

    # 2. 가계부 (PFM / account_app)
    pfm_running = containers.get("account_app", {}).get("state") == "running"
    # PFM HTTP login check (Internal IP 172.18.0.20 or localhost)
    pfm_http = check_http("http://172.18.0.20:8000/login/", expected_codes=(200,))
    pfm_ok = pfm_running and pfm_http["ok"]
    if not pfm_ok:
        issues.append(f"PFM(가계부) 상태 결함 (Running={pfm_running}, HTTP={pfm_http['code']})")

    domains["2. 가계부 (PFM)"] = {
        "ok": pfm_ok,
        "summary": f"account_app Up | /login/ HTTP {pfm_http['code']} ({pfm_http['latency_ms']}ms)"
    }

    # 3. 블로그 (Django Blog)
    blog_containers = ["django_blog_web", "django_blog_celery", "django_blog_beat"]
    blog_up = [c for c in blog_containers if containers.get(c, {}).get("state") == "running"]
    blog_http = check_http("http://127.0.0.1:8011/", expected_codes=(200,), headers={"X-Forwarded-Proto": "https", "Host": "localhost"})
    blog_ok = (len(blog_up) == len(blog_containers)) and blog_http["ok"]
    if not blog_ok:
        issues.append(f"블로그 상태 결함 ({len(blog_up)}/{len(blog_containers)} Up, HTTP={blog_http['code']})")

    domains["3. 블로그 (Django Blog)"] = {
        "ok": blog_ok,
        "summary": f"3대 컨테이너 {len(blog_up)}/3 Up | Port 8011 HTTP {blog_http['code']} ({blog_http['latency_ms']}ms)"
    }

    # 4. 오토포스트 (Autopost)
    autopost_containers = [
        "autopost_app", "autopost_celery_beat", "autopost_celery_worker_default",
        "autopost_celery_worker_generator", "autopost_celery_worker_delivery"
    ]
    autopost_up = [c for c in autopost_containers if containers.get(c, {}).get("state") == "running"]
    autopost_ok = len(autopost_up) == len(autopost_containers)
    if not autopost_ok:
        issues.append(f"오토포스트 컨테이너 결함 ({len(autopost_up)}/{len(autopost_containers)} Up)")

    domains["4. 오토포스트 (Autopost)"] = {
        "ok": autopost_ok,
        "summary": f"5대 컨테이너 {len(autopost_up)}/5 Up"
    }

    # 5. 인프라 DB & 웹 프록시
    infra_containers = ["postgres_db", "redis", "traefik", "nginx"]
    infra_up = [c for c in infra_containers if containers.get(c, {}).get("state") == "running"]
    infra_ok = len(infra_up) == len(infra_containers)
    if not infra_ok:
        issues.append(f"인프라 컨테이너 결함 ({len(infra_up)}/{len(infra_containers)} Up)")

    domains["5. 인프라 (DB & 프록시)"] = {
        "ok": infra_ok,
        "summary": f"Postgres/Redis/Nginx/Traefik {len(infra_up)}/4 Up"
    }

    # 6. 백업 시스템 (Backup)
    backup_stat = check_backup_status()
    if not backup_stat["ok"] and backup_stat.get("issue"):
        issues.append(backup_stat["issue"])

    domains["6. 백업 시스템 (Backup)"] = {
        "ok": backup_stat["ok"],
        "summary": backup_stat["summary"]
    }

    # 종합 판정 결정
    res_ok = resources["disk"]["ok"] and resources["memory"]["ok"] and resources["cpu"]["ok"]
    if not res_ok:
        issues.append("서버 자원(디스크/메모리/CPU) 임계치 초과")

    if not issues:
        overall = "HEALTHY"
    elif len(issues) == 1 and "자원" not in issues[0]:
        overall = "WARNING"
    else:
        overall = "CRITICAL"

    return {
        "timestamp": now_kst.strftime("%Y-%m-%d %H:%M:%S KST"),
        "overall": overall,
        "issues": issues,
        "domains": domains,
        "resources": resources
    }


def main():
    parser = argparse.ArgumentParser(description="The Prepared Server-Wide Health Monitor")
    parser.add_argument("--json", action="store_true", help="Output JSON format")
    parser.add_argument("--discord", action="store_true", help="Always send Discord alert")
    parser.add_argument("--discord-on-error", action="store_true", help="Send Discord alert only on WARNING/CRITICAL")
    args = parser.parse_args()

    report = run_full_server_inspection()

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print("=" * 64)
        print(f" [The Prepared] 서버 전체 통합 모니터링: {report['overall']}")
        print(f" Timestamp: {report['timestamp']}")
        print("=" * 64)
        for domain, data in report["domains"].items():
            status_tag = "[OK]     " if data["ok"] else "[WARNING]"
            print(f"  {status_tag} {domain}: {data['summary']}")
        
        res = report["resources"]
        print("-" * 64)
        print(f"  [자원 상태] 디스크: {res['disk']['used_pct']}% 사용 ({res['disk']['free_gb']}GB 남음) | "
              f"RAM: {res['memory']['avail_gb']}GB 여유 | CPU 로드: {res['cpu']['load_5m']}")
        print("=" * 64)

        if report["issues"]:
            print(f"\n⚠️ 감지된 이슈 ({len(report['issues'])}건):")
            for iss in report["issues"]:
                print(f"  - {iss}")

    webhook_url = load_env_var("DISCORD_WEBHOOK_URL")
    if args.discord or (args.discord_on_error and report["overall"] != "HEALTHY"):
        send_discord_alert(report, webhook_url)


if __name__ == "__main__":
    main()
