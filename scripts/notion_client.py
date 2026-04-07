#!/usr/bin/env python3
"""Notion API client for Time Boxing system."""

import json
import sys
import os
from datetime import datetime, date, timedelta
from pathlib import Path

import requests

CONFIG_PATH = Path(__file__).parent.parent / "config.json"
NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


# === 설정 ===

def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def save_config(config):
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    print(f"Config saved to {CONFIG_PATH}")


def _headers(config=None):
    if config is None:
        config = load_config()
    return {
        "Authorization": f"Bearer {config['notion_api_key']}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }


def _notion_get(endpoint, config=None):
    r = requests.get(f"{NOTION_API}{endpoint}", headers=_headers(config))
    r.raise_for_status()
    return r.json()


def _notion_post(endpoint, data, config=None):
    r = requests.post(f"{NOTION_API}{endpoint}", headers=_headers(config), json=data)
    r.raise_for_status()
    return r.json()


def _notion_patch(endpoint, data, config=None):
    r = requests.patch(f"{NOTION_API}{endpoint}", headers=_headers(config), json=data)
    r.raise_for_status()
    return r.json()


# === 초기 세팅 ===

def setup_databases():
    """타임박싱 DB + 주간 회고 DB 생성, ID를 config에 저장."""
    config = load_config()
    parent_id = config["parent_page_id"]

    # 타임박싱 DB
    if not config["databases"].get("timebox"):
        print("Creating Timebox DB...")
        tb_db = _notion_post("/databases", {
            "parent": {"type": "page_id", "page_id": parent_id},
            "icon": {"type": "emoji", "emoji": "⏰"},
            "title": [{"type": "text", "text": {"content": "타임박싱"}}],
            "properties": {
                "날짜": {"title": {}},
                "일자": {"date": {}},
                "상태": {"select": {"options": [
                    {"name": "계획됨", "color": "yellow"},
                    {"name": "진행중", "color": "blue"},
                    {"name": "완료", "color": "green"},
                ]}},
                "총 블록": {"number": {"format": "number"}},
                "완료 블록": {"number": {"format": "number"}},
                "달성률": {"number": {"format": "percent"}},
                "핵심 업무": {"multi_select": {"options": []}},
                "메모": {"rich_text": {}},
            }
        }, config)
        config["databases"]["timebox"] = tb_db["id"]
        print(f"  Timebox DB created: {tb_db['id']}")

    # 주간 회고 DB
    if not config["databases"].get("weekly_review"):
        print("Creating Weekly Review DB...")
        wr_db = _notion_post("/databases", {
            "parent": {"type": "page_id", "page_id": parent_id},
            "icon": {"type": "emoji", "emoji": "📊"},
            "title": [{"type": "text", "text": {"content": "주간 회고"}}],
            "properties": {
                "주차": {"title": {}},
                "기간": {"date": {}},
                "평균 달성률": {"number": {"format": "percent"}},
                "총 완료 블록": {"number": {"format": "number"}},
                "총 계획 블록": {"number": {"format": "number"}},
                "주요 성과": {"rich_text": {}},
                "개선점": {"rich_text": {}},
            }
        }, config)
        config["databases"]["weekly_review"] = wr_db["id"]
        print(f"  Weekly Review DB created: {wr_db['id']}")

    save_config(config)

    # 업무 TODO DB에 추가 속성 설정
    add_todo_properties()

    print("Setup complete!")


# === 업무 TODO 연동 ===

def cleanup_previous_day():
    """전날 잔여 데이터 정리 (아침 계획 시 호출).

    Do Today가 체크된 업무 중 check-in/out이나 배정 시간이 남아있으면 초기화하고,
    완료 상태가 아닌 업무의 Do Today를 해제한다.
    """
    config = load_config()
    db_id = config["databases"]["todo"]
    result = _notion_post(f"/databases/{db_id}/query", {
        "filter": {"property": "Do Today", "checkbox": {"equals": True}}
    }, config)
    cleaned = 0
    for page in result.get("results", []):
        props = page["properties"]
        has_checkin = props.get("check-in", {}).get("date") is not None
        has_checkout = props.get("check-out", {}).get("date") is not None
        has_assigned = props.get("배정 시간", {}).get("date") is not None

        if has_checkin or has_checkout or has_assigned:
            clear_assigned_time(page["id"])
            cleaned += 1

        # Do Today 해제
        set_do_today(page["id"], False)

    print(f"Cleanup done: {cleaned} tasks cleared, all Do Today unchecked")


def get_today_tasks():
    """'Do Today' = true인 업무 조회."""
    config = load_config()
    db_id = config["databases"]["todo"]
    result = _notion_post(f"/databases/{db_id}/query", {
        "filter": {
            "property": "Do Today",
            "checkbox": {"equals": True}
        }
    }, config)
    tasks = []
    for page in result.get("results", []):
        props = page["properties"]
        tasks.append({
            "id": page["id"],
            "name": _get_title(props.get("이름", props.get("Name", {}))),
            "status": _get_status(props.get("상태", props.get("Status", {}))),
            "priority": _get_select(props.get("Priority", {})),
            "issue_type": _get_select(props.get("이슈 유형", {})),
            "category": _get_select(props.get("분류", {})),
            "assigned_time": props.get("배정 시간", {}).get("date"),
            "checkin": props.get("check-in", {}).get("date"),
            "checkout": props.get("check-out", {}).get("date"),
            "execution_log": _get_rich_text(props.get("실행 이력", {})),
        })
    return tasks


def get_backlog_tasks():
    """상태가 Todo/Open/In progress인 전체 업무 조회 (Dump 단계용)."""
    config = load_config()
    db_id = config["databases"]["todo"]
    result = _notion_post(f"/databases/{db_id}/query", {
        "filter": {
            "or": [
                {"property": "상태", "status": {"equals": "Todo"}},
                {"property": "상태", "status": {"equals": "Open"}},
                {"property": "상태", "status": {"equals": "In progress"}},
                {"property": "상태", "status": {"equals": "Holding"}},
            ]
        }
    }, config)
    tasks = []
    for page in result.get("results", []):
        props = page["properties"]
        tasks.append({
            "id": page["id"],
            "name": _get_title(props.get("이름", props.get("Name", {}))),
            "status": _get_status(props.get("상태", props.get("Status", {}))),
            "priority": _get_select(props.get("Priority", {})),
            "issue_type": _get_select(props.get("이슈 유형", {})),
            "category": _get_select(props.get("분류", {})),
            "do_today": _get_checkbox(props.get("Do Today", {})),
        })
    return tasks


def add_task_to_todo(name, props=None):
    """새 업무를 업무 TODO DB에 추가."""
    config = load_config()
    db_id = config["databases"]["todo"]
    properties = {
        "이름": {"title": [{"text": {"content": name}}]},
    }
    if props:
        if props.get("priority"):
            properties["Priority"] = {"select": {"name": props["priority"]}}
        if props.get("issue_type"):
            properties["이슈 유형"] = {"select": {"name": props["issue_type"]}}
        if props.get("category"):
            if isinstance(props["category"], list):
                properties["분류"] = {"multi_select": [{"name": c} for c in props["category"]]}
            else:
                properties["분류"] = {"multi_select": [{"name": props["category"]}]}
        if props.get("do_today"):
            properties["Do Today"] = {"checkbox": True}
    result = _notion_post("/pages", {
        "parent": {"database_id": db_id},
        "properties": properties,
    }, config)
    print(f"Task added: {name} ({result['id']})")
    return result["id"]


def set_do_today(page_id, checked):
    """'Do Today' 체크박스 업데이트."""
    config = load_config()
    _notion_patch(f"/pages/{page_id}", {
        "properties": {"Do Today": {"checkbox": checked}}
    }, config)
    print(f"Do Today {'checked' if checked else 'unchecked'}: {page_id}")


# === 업무 TODO 속성 관리 ===

def add_todo_properties():
    """업무 TODO DB에 '배정 시간', '실행 이력', 'check-in', 'check-out' 속성 추가 (최초 1회)."""
    config = load_config()
    db_id = config["databases"]["todo"]
    _notion_patch(f"/databases/{db_id}", {
        "properties": {
            "배정 시간": {"date": {}},
            "실행 이력": {"rich_text": {}},
            "check-in": {"date": {}},
            "check-out": {"date": {}},
        }
    }, config)
    print("TODO DB properties added: 배정 시간(date), 실행 이력(rich_text), check-in(date), check-out(date)")


def set_assigned_time(page_id, start_iso, end_iso):
    """배정 시간 설정 (date range).

    Args:
        page_id: 업무 TODO 페이지 ID
        start_iso: 시작 시각 ISO 형식 (예: '2026-03-09T09:00:00+09:00')
        end_iso: 종료 시각 ISO 형식 (예: '2026-03-09T10:30:00+09:00')
    """
    config = load_config()
    _notion_patch(f"/pages/{page_id}", {
        "properties": {
            "배정 시간": {"date": {"start": start_iso, "end": end_iso}}
        }
    }, config)
    print(f"Assigned time set: {page_id} → {start_iso} ~ {end_iso}")


def clear_assigned_time(page_id):
    """배정 시간, check-in, check-out 초기화."""
    config = load_config()
    _notion_patch(f"/pages/{page_id}", {
        "properties": {
            "배정 시간": {"date": None},
            "check-in": {"date": None},
            "check-out": {"date": None},
        }
    }, config)
    print(f"Assigned time & check-in/out cleared: {page_id}")


def append_execution_log(page_id, date_str, blocks):
    """실행 이력에 추가 (예: 기존 '3/4(2블록)' → '3/4(2블록), 3/5(4블록)').

    Args:
        page_id: 업무 TODO 페이지 ID
        date_str: 날짜 (예: '3/5')
        blocks: 완료 블록 수
    """
    config = load_config()
    # 기존 실행 이력 조회
    page = _notion_get(f"/pages/{page_id}", config)
    existing = _get_rich_text(page["properties"].get("실행 이력", {}))
    new_entry = f"{date_str}({blocks}블록)"
    if existing:
        updated = f"{existing}, {new_entry}"
    else:
        updated = new_entry
    _notion_patch(f"/pages/{page_id}", {
        "properties": {
            "실행 이력": {"rich_text": [{"type": "text", "text": {"content": updated}}]}
        }
    }, config)
    print(f"Execution log appended: {page_id} → {updated}")


# === 타임박싱 일일 관리 ===

def setup_timebox_page(date_str, total_blocks, key_tasks=None, task_assignments=None):
    """템플릿으로 생성된 타임박싱 페이지를 찾아서 속성 + 배정 시간을 채움.

    사전 조건: 사용자가 Notion에서 템플릿으로 오늘 페이지를 먼저 생성해야 함.

    Args:
        date_str: "2026-03-05" 형태
        total_blocks: 총 블록 수
        key_tasks: ["API 설계", "코드 리뷰"] 핵심 업무 태그 목록
        task_assignments: [{"page_id": "...", "start_iso": "...", "end_iso": "..."}, ...] 업무별 배정 시간
    """
    config = load_config()

    # 오늘 페이지 찾기 (템플릿으로 생성된 페이지)
    timebox = get_today_timebox(date_str)
    if timebox:
        page_id = timebox["page_id"]
    else:
        # 날짜 미설정된 최신 페이지 찾기 (방금 템플릿으로 만든 페이지)
        db_id = config["databases"]["timebox"]
        result = _notion_post(f"/databases/{db_id}/query", {
            "sorts": [{"timestamp": "created_time", "direction": "descending"}],
            "page_size": 1,
        }, config)
        pages = result.get("results", [])
        if not pages:
            print("Error: 타임박싱 페이지를 찾을 수 없습니다. Notion에서 템플릿으로 먼저 생성해주세요.")
            return None
        page_id = pages[0]["id"]

    dt = datetime.strptime(date_str, "%Y-%m-%d")
    weekdays = ["월", "화", "수", "목", "금", "토", "일"]
    title = f"{date_str} ({weekdays[dt.weekday()]})"

    # 속성 업데이트
    properties = {
        "날짜": {"title": [{"text": {"content": title}}]},
        "일자": {"date": {"start": date_str}},
        "상태": {"select": {"name": "계획됨"}},
        "총 블록": {"number": total_blocks},
        "완료 블록": {"number": 0},
        "달성률": {"number": 0},
    }
    if key_tasks:
        properties["핵심 업무"] = {
            "multi_select": [{"name": t} for t in key_tasks]
        }

    _notion_patch(f"/pages/{page_id}", {"properties": properties}, config)

    # 업무별 배정 시간 설정
    if task_assignments:
        for assignment in task_assignments:
            set_assigned_time(assignment["page_id"], assignment["start_iso"], assignment["end_iso"])

    page_url = f"https://notion.so/{page_id.replace('-', '')}"
    print(f"Timebox page setup: {title}")
    print(f"URL: {page_url}")
    return {"page_id": page_id, "url": page_url}


def get_today_timebox(date_str=None):
    """오늘자 타임박싱 페이지 조회."""
    config = load_config()
    db_id = config["databases"]["timebox"]
    if not db_id:
        return None
    if date_str is None:
        date_str = date.today().isoformat()

    result = _notion_post(f"/databases/{db_id}/query", {
        "filter": {
            "property": "일자",
            "date": {"equals": date_str}
        }
    }, config)
    pages = result.get("results", [])
    if not pages:
        return None

    page = pages[0]
    props = page["properties"]
    return {
        "page_id": page["id"],
        "url": page.get("url", ""),
        "title": _get_title(props.get("날짜", {})),
        "status": _get_select(props.get("상태", {})),
        "total_blocks": _get_number(props.get("총 블록", {})),
        "completed_blocks": _get_number(props.get("완료 블록", {})),
        "achievement": _get_number(props.get("달성률", {})),
    }


def _get_page_blocks(page_id, config=None):
    """페이지의 모든 블록을 조회."""
    if config is None:
        config = load_config()
    blocks = []
    url = f"/blocks/{page_id}/children?page_size=100"
    while url:
        result = _notion_get(url, config)
        blocks.extend(result.get("results", []))
        if result.get("has_more"):
            url = f"/blocks/{page_id}/children?page_size=100&start_cursor={result['next_cursor']}"
        else:
            url = None
    return blocks


def update_timebox_checkin(page_id, checkin_note):
    """체크인 로그 추가.

    Args:
        page_id: 타임박싱 페이지 ID
        checkin_note: 체크인 메모 (예: "API 설계 완료, 예상보다 15분 초과")
    """
    config = load_config()
    blocks = _get_page_blocks(page_id, config)

    # 체크인 로그 heading 찾기
    checkin_heading_id = None
    for b in blocks:
        if b["type"] == "heading_2":
            text = "".join(t.get("plain_text", "") for t in b["heading_2"]["rich_text"])
            if "체크인 로그" in text:
                checkin_heading_id = b["id"]
                break

    if checkin_heading_id:
        now = datetime.now().strftime("%H:%M")
        # heading 뒤에 블록 삽입 (after 파라미터 사용)
        _notion_patch(f"/blocks/{page_id}/children", {
            "children": [{
                "object": "block", "type": "callout", "callout": {
                    "icon": {"type": "emoji", "emoji": "📌"},
                    "rich_text": [{"type": "text", "text": {
                        "content": f"{now} — {checkin_note}"
                    }}],
                }
            }],
            "after": checkin_heading_id,
        }, config)

    # 상태를 진행중으로 변경
    _notion_patch(f"/pages/{page_id}", {
        "properties": {
            "상태": {"select": {"name": "진행중"}},
        }
    }, config)

    print(f"Check-in logged: {checkin_note}")
    return {"note": checkin_note}


def finalize_timebox(page_id, user_review="", date_str=None):
    """하루 마무리 리뷰.

    TODO DB의 Do Today 업무들에서 배정/실제 블록을 계산하고,
    리뷰 섹션에 오늘 요약 + 업무별 실행 + 미완료 업무 + 한줄 회고를 작성.

    Args:
        page_id: 타임박싱 페이지 ID
        user_review: 사용자 한줄 회고
        date_str: 날짜 "2026-03-05" (None이면 오늘)
    """
    config = load_config()
    db_id = config["databases"]["todo"]
    dt = datetime.strptime(date_str, "%Y-%m-%d") if date_str else datetime.now()
    short_date = f"{dt.month}/{dt.day}"

    # Do Today 업무 조회
    result = _notion_post(f"/databases/{db_id}/query", {
        "filter": {"property": "Do Today", "checkbox": {"equals": True}}
    }, config)

    total_assigned_blocks = 0
    total_actual_blocks = 0
    task_results = []
    incomplete_tasks = []

    for page in result.get("results", []):
        props = page["properties"]
        name = _get_title(props.get("이름", props.get("Name", {})))
        assigned_blocks = _calc_blocks_from_date_range(props.get("배정 시간", {}))
        actual_blocks = _calc_blocks_from_checkin_out(
            props.get("check-in", {}),
            props.get("check-out", {})
        )
        total_assigned_blocks += assigned_blocks
        total_actual_blocks += actual_blocks

        # 상태 판정
        if actual_blocks == 0:
            status = "❌"
            incomplete_tasks.append(name)
        elif actual_blocks < assigned_blocks:
            status = "미완료"
            incomplete_tasks.append(name)
        elif actual_blocks > assigned_blocks:
            status = "⚠️ 초과"
        else:
            status = "✅"

        task_results.append({
            "name": name,
            "assigned": assigned_blocks,
            "actual": actual_blocks,
            "status": status,
        })

        # 실행 이력 누적
        if actual_blocks > 0:
            append_execution_log(page["id"], short_date, actual_blocks)

        # 배정 시간 + check-in/out 초기화
        clear_assigned_time(page["id"])

    # 달성률 계산
    completed_blocks = sum(min(t["actual"], t["assigned"]) for t in task_results)
    achievement = round(completed_blocks / total_assigned_blocks, 2) if total_assigned_blocks else 0

    # 타임박싱 페이지 속성 업데이트 (상태는 select 타입)
    _notion_patch(f"/pages/{page_id}", {
        "properties": {
            "총 블록": {"number": total_assigned_blocks},
            "완료 블록": {"number": completed_blocks},
            "달성률": {"number": achievement},
            "상태": {"select": {"name": "완료"}},
        }
    }, config)

    # 리뷰 섹션에 내용 추가
    blocks = _get_page_blocks(page_id, config)
    review_heading_id = None
    for b in blocks:
        if b["type"] == "heading_2":
            text = "".join(t.get("plain_text", "") for t in b["heading_2"]["rich_text"])
            if "리뷰" in text and "체크인" not in text:
                review_heading_id = b["id"]
                break

    if review_heading_id:
        review_children = []

        # 오늘 요약
        assigned_hours = total_assigned_blocks * 15
        actual_hours = total_actual_blocks * 15
        review_children.append(_make_paragraph(f"━━ 오늘 요약 ━━"))
        review_children.append(_make_paragraph(
            f"총 배정: {total_assigned_blocks}블록 ({assigned_hours // 60}시간 {assigned_hours % 60}분)\n"
            f"총 실행: {total_actual_blocks}블록 ({actual_hours // 60}시간 {actual_hours % 60}분)\n"
            f"달성률: {achievement * 100:.0f}% ({completed_blocks}/{total_assigned_blocks} 완료)"
        ))

        # 업무별 실행
        review_children.append(_make_paragraph(f"━━ 업무별 실행 ━━"))
        for t in task_results:
            review_children.append(_make_paragraph(
                f"• {t['name']}: 배정 {t['assigned']} → 실제 {t['actual']} {t['status']}"
            ))

        # 미완료 업무
        if incomplete_tasks:
            review_children.append(_make_paragraph(f"━━ 미완료 업무 ━━"))
            for name in incomplete_tasks:
                review_children.append(_make_paragraph(f"• {name} → 내일 이월"))

        # 한줄 회고
        review_children.append(_make_paragraph(f"━━ 한줄 회고 ━━"))
        review_children.append(_make_paragraph(user_review if user_review else "(작성 필요)"))

        # heading 블록은 children을 지원하지 않으므로, 페이지에 after로 추가
        _notion_patch(f"/blocks/{page_id}/children", {
            "children": review_children,
            "after": review_heading_id,
        }, config)

    print(f"Finalized: {completed_blocks}/{total_assigned_blocks} ({achievement * 100:.0f}%)")
    return {
        "completed": completed_blocks,
        "total": total_assigned_blocks,
        "actual_total": total_actual_blocks,
        "achievement": achievement,
        "task_results": task_results,
        "incomplete_tasks": incomplete_tasks,
    }


def _make_paragraph(text):
    """간단한 paragraph 블록 생성 헬퍼."""
    return {
        "object": "block", "type": "paragraph", "paragraph": {
            "rich_text": [{"type": "text", "text": {"content": text}}]
        }
    }


# === 주간 회고 ===

def create_weekly_review(week_start_str=None):
    """주간 회고 페이지 생성.

    Args:
        week_start_str: 주간 시작일 "2026-03-02" (None이면 이번 주 월요일)
    """
    config = load_config()
    timebox_db = config["databases"]["timebox"]
    review_db = config["databases"]["weekly_review"]

    if week_start_str is None:
        today = date.today()
        week_start = today - timedelta(days=today.weekday())
    else:
        week_start = datetime.strptime(week_start_str, "%Y-%m-%d").date()

    week_end = week_start + timedelta(days=4)  # 금요일
    iso_year, iso_week, _ = week_start.isocalendar()

    # 해당 주간 타임박싱 페이지들 조회
    result = _notion_post(f"/databases/{timebox_db}/query", {
        "filter": {
            "and": [
                {"property": "일자", "date": {"on_or_after": week_start.isoformat()}},
                {"property": "일자", "date": {"on_or_before": week_end.isoformat()}},
            ]
        }
    }, config)

    pages = result.get("results", [])
    total_planned = 0
    total_completed = 0
    achievements = []

    for page in pages:
        props = page["properties"]
        planned = _get_number(props.get("총 블록", {})) or 0
        completed = _get_number(props.get("완료 블록", {})) or 0
        total_planned += planned
        total_completed += completed
        ach = _get_number(props.get("달성률", {}))
        if ach is not None:
            achievements.append(ach)

    avg_achievement = round(sum(achievements) / len(achievements), 2) if achievements else 0

    title = f"{iso_year}-W{iso_week:02d} ({week_start.strftime('%m/%d')}~{week_end.strftime('%m/%d')})"

    page = _notion_post("/pages", {
        "parent": {"database_id": review_db},
        "properties": {
            "주차": {"title": [{"text": {"content": title}}]},
            "기간": {"date": {"start": week_start.isoformat(), "end": week_end.isoformat()}},
            "평균 달성률": {"number": avg_achievement},
            "총 완료 블록": {"number": total_completed},
            "총 계획 블록": {"number": total_planned},
        }
    }, config)

    # 페이지 내부에 요약 추가
    summary_children = [
        {"object": "block", "type": "heading_2", "heading_2": {
            "rich_text": [{"type": "text", "text": {"content": "📈 주간 통계"}}]
        }},
        {"object": "block", "type": "paragraph", "paragraph": {
            "rich_text": [{"type": "text", "text": {
                "content": f"기록된 일수: {len(pages)}일\n"
                           f"총 계획 블록: {total_planned}개 ({total_planned * 15}분)\n"
                           f"총 완료 블록: {total_completed}개 ({total_completed * 15}분)\n"
                           f"평균 달성률: {avg_achievement*100:.0f}%"
            }}]
        }},
        {"object": "block", "type": "heading_2", "heading_2": {
            "rich_text": [{"type": "text", "text": {"content": "🏆 주요 성과"}}]
        }},
        {"object": "block", "type": "paragraph", "paragraph": {
            "rich_text": [{"type": "text", "text": {"content": "(여기에 주요 성과를 작성하세요)"}}]
        }},
        {"object": "block", "type": "heading_2", "heading_2": {
            "rich_text": [{"type": "text", "text": {"content": "💡 개선점"}}]
        }},
        {"object": "block", "type": "paragraph", "paragraph": {
            "rich_text": [{"type": "text", "text": {"content": "(여기에 개선점을 작성하세요)"}}]
        }},
    ]

    # 업무별 실행 이력 수집
    todo_db = config["databases"]["todo"]
    todo_result = _notion_post(f"/databases/{todo_db}/query", {
        "filter": {
            "property": "실행 이력",
            "rich_text": {"is_not_empty": True}
        }
    }, config)
    exec_tasks = todo_result.get("results", [])
    if exec_tasks:
        summary_children.append({
            "object": "block", "type": "heading_2", "heading_2": {
                "rich_text": [{"type": "text", "text": {"content": "📋 업무별 실행 이력"}}]
            }
        })
        for t in exec_tasks:
            t_props = t["properties"]
            t_name = _get_title(t_props.get("이름", t_props.get("Name", {})))
            t_log = _get_rich_text(t_props.get("실행 이력", {}))
            if t_log:
                summary_children.append({
                    "object": "block", "type": "bulleted_list_item",
                    "bulleted_list_item": {
                        "rich_text": [{"type": "text", "text": {
                            "content": f"{t_name}: {t_log}"
                        }}]
                    }
                })

    _notion_patch(f"/blocks/{page['id']}/children", {
        "children": summary_children
    }, config)

    page_url = page.get("url", "")
    print(f"Weekly review created: {title}")
    print(f"  Days recorded: {len(pages)}")
    print(f"  Avg achievement: {avg_achievement*100:.0f}%")
    print(f"  URL: {page_url}")
    return {"page_id": page["id"], "url": page_url, "stats": {
        "days": len(pages), "total_planned": total_planned,
        "total_completed": total_completed, "avg_achievement": avg_achievement,
    }}


# === 유틸 ===

def _get_title(prop):
    items = prop.get("title", [])
    return "".join(t.get("plain_text", "") for t in items) if items else ""


def _get_select(prop):
    sel = prop.get("select") or prop.get("status")
    return sel.get("name") if sel else None


def _get_status(prop):
    s = prop.get("status")
    return s.get("name") if s else None


def _get_number(prop):
    return prop.get("number")


def _get_checkbox(prop):
    return prop.get("checkbox", False)


def _get_rich_text(prop):
    items = prop.get("rich_text", [])
    return "".join(t.get("plain_text", "") for t in items) if items else ""


def _calc_blocks_from_date_range(date_prop):
    """date 속성에서 블록 수 계산 (15분 단위)."""
    if not date_prop or not date_prop.get("date"):
        return 0
    d = date_prop["date"]
    start_str = d.get("start")
    end_str = d.get("end")
    if not start_str or not end_str:
        return 0
    try:
        start = datetime.fromisoformat(start_str)
        end = datetime.fromisoformat(end_str)
        minutes = (end - start).total_seconds() // 60
        return int(minutes // 15)
    except (ValueError, AttributeError):
        return 0


def _calc_blocks_from_checkin_out(checkin_prop, checkout_prop):
    """check-in/check-out date 속성에서 실제 실행 블록 수 계산.

    Notion에서 check-in/check-out 시각의 타임존이 혼재될 수 있으므로
    (+09:00 vs +00:00) 타임존을 무시하고 로컬 시각(naive) 기준으로 계산한다.
    """
    if not checkin_prop or not checkout_prop:
        return 0
    checkin_date = checkin_prop.get("date")
    checkout_date = checkout_prop.get("date")
    if not checkin_date or not checkout_date:
        return 0
    start_str = checkin_date.get("start")
    end_str = checkout_date.get("start")
    if not start_str or not end_str:
        return 0
    try:
        start = datetime.fromisoformat(start_str).replace(tzinfo=None)
        end = datetime.fromisoformat(end_str).replace(tzinfo=None)
        minutes = (end - start).total_seconds() // 60
        if minutes < 0:
            return 0
        return int(minutes // 15)
    except (ValueError, AttributeError):
        return 0


def test_connection():
    """API 연결 테스트."""
    config = load_config()
    try:
        user = _notion_get("/users/me", config)
        print(f"Connected as: {user.get('name', 'Unknown')}")
        print(f"Bot type: {user.get('type', 'Unknown')}")

        # TODO DB 접근 테스트
        db_id = config["databases"]["todo"]
        db = _notion_get(f"/databases/{db_id}", config)
        print(f"TODO DB: {db.get('title', [{}])[0].get('plain_text', 'Unknown')}")
        print(f"  Properties: {', '.join(db.get('properties', {}).keys())}")

        # Timebox DB 체크
        tb_id = config["databases"].get("timebox")
        if tb_id:
            tb_db = _notion_get(f"/databases/{tb_id}", config)
            print(f"Timebox DB: OK ({tb_id})")
        else:
            print("Timebox DB: Not created yet (run 'setup')")

        # Weekly Review DB 체크
        wr_id = config["databases"].get("weekly_review")
        if wr_id:
            print(f"Weekly Review DB: OK ({wr_id})")
        else:
            print("Weekly Review DB: Not created yet (run 'setup')")

        print("\nAll checks passed!")
        return True
    except Exception as e:
        print(f"Connection failed: {e}")
        return False


# === CLI ===

def main():
    if len(sys.argv) < 2:
        print("Usage: python notion_client.py <command>")
        print("Commands: test, setup, today-tasks, backlog, create-test")
        return

    cmd = sys.argv[1]

    if cmd == "test":
        test_connection()
    elif cmd == "cleanup":
        cleanup_previous_day()
    elif cmd == "setup":
        setup_databases()
    elif cmd == "today-tasks":
        tasks = get_today_tasks()
        for t in tasks:
            time_info = f" | {t['assigned_time']}" if t.get('assigned_time') else ""
            log_info = f" | 이력: {t['execution_log']}" if t.get('execution_log') else ""
            print(f"  [{t['priority'] or '-'}] {t['name']} ({t['status']}){time_info}{log_info}")
        if not tasks:
            print("  No 'Do Today' tasks found.")
    elif cmd == "backlog":
        tasks = get_backlog_tasks()
        for t in tasks:
            flag = "✓" if t.get("do_today") else " "
            print(f"  [{flag}] [{t['priority'] or '-'}] {t['name']} ({t['status']})")
        print(f"  Total: {len(tasks)} tasks")
    elif cmd == "create-test":
        # 테스트용 타임박싱 페이지 생성
        today = date.today().isoformat()
        items = [
            {"time": "09:00-09:15", "label": "[테스트] 연결 확인", "is_buffer": False},
            {"time": "09:15-09:30", "label": "[테스트] 기능 점검", "is_buffer": False},
            {"time": "09:30-09:45", "label": "버퍼", "is_buffer": True},
        ]
        create_timebox_page(today, items, 3, ["테스트"])
    else:
        print(f"Unknown command: {cmd}")


if __name__ == "__main__":
    main()
