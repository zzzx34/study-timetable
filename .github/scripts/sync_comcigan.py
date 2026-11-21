#!/usr/bin/env python3
"""컴시간 알리미 시간표를 Supabase planner_kv 로 동기화한다.

- 학교 이름·학년·반은 저장소 파일에 남기지 않는다. GitHub Actions 시크릿
  (COMCIGAN_SCHOOL_NAME / COMCIGAN_GRADE / COMCIGAN_CLASS)에서만 읽는다.
- 결과 JSON 에도 학교명·학교코드·학년·반을 넣지 않는다.
- 이번 주(week_num=0)와 다음 주(week_num=1) 시간표를 함께 저장한다.

저장 위치: public.planner_kv, id = unyango_v5_comcigan
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

SB_URL = 'https://ulaxmsssquhchwuxxdeq.supabase.co'
SB_KEY = 'sb_publishable_8jVnRXyQKvvQp73TaKgaug_2WLNWwPL'
TABLE = 'planner_kv'
ROW_ID = 'unyango_v5_comcigan'

KST = dt.timezone(dt.timedelta(hours=9))
DAY_LABELS = ['월', '화', '수', '목', '금']


def log(msg: str) -> None:
    print(msg, flush=True)


def fail(msg: str) -> 'None':
    print(f'::error::{msg}', file=sys.stderr, flush=True)
    sys.exit(1)


def env_value(name: str) -> str:
    value = (os.environ.get(name) or '').strip()
    if not value:
        fail(
            f'{name} 시크릿이 비어 있습니다. 저장소 Settings → Secrets and variables → '
            'Actions 에서 값을 등록한 뒤 다시 실행해 주세요.'
        )
    return value


def env_int(name: str) -> int:
    raw = env_value(name)
    try:
        return int(raw)
    except ValueError:
        fail(f'{name} 값이 숫자가 아닙니다.')


def monday_of(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def parse_period_times(day_time) -> list:
    """일과시간 응답에서 교시별 시작·종료 시각만 뽑는다 (형식이 다르면 빈 목록).

    실제 응답은 ["1(09:10)", "2(10:10)", ...] 처럼 문자열로 오고, 예전 형식은
    [[1, "09:10", "10:00"], ...] 같은 2차원 배열이었다. 둘 다 받아준다.
    """
    rows = []
    if not isinstance(day_time, (list, tuple)):
        return rows
    for row in day_time:
        if isinstance(row, str):
            match = re.match(
                r'^\s*(\d{1,2})\s*\(\s*(\d{1,2}:\d{2})\s*(?:[~\-]\s*(\d{1,2}:\d{2}))?\s*\)',
                row,
            )
            if match:
                entry = {'p': int(match.group(1)), 'start': match.group(2)}
                if match.group(3):
                    entry['end'] = match.group(3)
                if 1 <= entry['p'] <= 12:
                    rows.append(entry)
            continue
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        try:
            period = int(row[0])
        except (TypeError, ValueError):
            continue
        times = [
            str(cell)[:5]
            for cell in row[1:]
            if isinstance(cell, str) and re.match(r'^\d{1,2}:\d{2}$', str(cell)[:5])
        ]
        if 1 <= period <= 12 and len(times) >= 2:
            rows.append({'p': period, 'start': times[0], 'end': times[1]})
    rows.sort(key=lambda item: item['p'])
    return rows


def build_week(tt, grade: int, cls: int, monday: dt.date, week_id: str):
    """한 주치 시간표를 앱이 그대로 그릴 수 있는 최소 구조로 만든다."""
    try:
        days = tt.timetable[grade][cls]
    except (IndexError, TypeError):
        fail('컴시간 응답에 해당 학년·반 시간표가 없습니다. 학년·반 설정을 확인해 주세요.')

    out_days = []
    max_period = 0
    total = 0
    for day_idx in range(1, 6):
        periods = []
        if day_idx < len(days):
            for item in days[day_idx]:
                subject = (item.subject or '').strip()
                if not subject:
                    continue
                row = {'p': int(item.period), 's': subject}
                teacher = (item.teacher or '').strip()
                if teacher:
                    row['t'] = teacher
                if item.replaced:
                    row['c'] = 1
                    original = getattr(item, 'original', None)
                    if original is not None and (original.subject or '').strip():
                        row['ps'] = original.subject.strip()
                        if (original.teacher or '').strip():
                            row['pt'] = original.teacher.strip()
                periods.append(row)
                total += 1
                max_period = max(max_period, row['p'])
        out_days.append({'d': day_idx - 1, 'periods': periods})

    return {
        'id': week_id,
        'monday': monday.isoformat(),
        'max_p': max_period,
        'days': out_days,
    }, total


def resolve_school_codes(school_name: str):
    """학교 검색 결과에서 정확히 일치하는 학교를 찾는다 (동명 학교 대비)."""
    from pycomcigan.helpers import get_comcigan_response_for_codes, school_search
    from pycomcigan.service_page import decode_service_page
    from pycomcigan.service_protocol import parse_service_protocol

    response = get_comcigan_response_for_codes()
    protocol = parse_service_protocol(decode_service_page(response.text))
    rows = school_search(school_name, protocol.endpoint, protocol)

    exact = [row for row in rows if len(row) >= 4 and row[2] == school_name]
    candidates = exact or [row for row in rows if len(row) >= 4]
    if not candidates:
        fail('컴시간에서 학교를 찾지 못했습니다. 학교 이름 시크릿을 확인해 주세요.')
    if len(candidates) > 1:
        fail(f'같은 이름의 학교가 {len(candidates)}곳입니다. 학교 이름을 더 정확히 지정해 주세요.')
    row = candidates[0]
    return int(row[0]), int(row[3])


def fetch_week(school_name: str, local_code: int, school_code: int, week_num: int):
    from pycomcigan import TimeTable

    last_error = None
    for attempt in range(3):
        try:
            if week_num == 0:
                return TimeTable(school_name, local_code=local_code, school_code=school_code, week_num=0)
            time.sleep(2)
            return TimeTable(school_name, local_code=local_code, school_code=school_code, week_num=week_num)
        except RuntimeError as error:
            last_error = error
            message = str(error)
            if 'rate limit' in message or '403' in message or '404' in message.lower():
                time.sleep(5 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = error
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f'컴시간 서버 요청이 실패했습니다: {last_error}')


def upsert(payload: dict) -> None:
    body = json.dumps([{'id': ROW_ID, 'value': payload}], ensure_ascii=False).encode('utf-8')
    request = urllib.request.Request(
        f'{SB_URL}/rest/v1/{TABLE}?on_conflict=id',
        data=body,
        method='POST',
        headers={
            'apikey': SB_KEY,
            'Authorization': f'Bearer {SB_KEY}',
            'Content-Type': 'application/json',
            'Prefer': 'resolution=merge-duplicates,return=minimal',
        },
    )
    last_error = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status not in (200, 201, 204):
                    raise RuntimeError(f'HTTP {response.status}')
                return
        except urllib.error.HTTPError as error:
            last_error = f'HTTP {error.code} {error.read()[:200]!r}'
            if error.code in (400, 401, 403):
                break
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = error
        time.sleep(3 * (attempt + 1))
    fail(f'Supabase 저장에 실패했습니다: {last_error}')


def main() -> None:
    school_name = env_value('COMCIGAN_SCHOOL_NAME')
    grade = env_int('COMCIGAN_GRADE')
    cls = env_int('COMCIGAN_CLASS')

    today = dt.datetime.now(KST).date()
    this_monday = monday_of(today)

    local_code, school_code = resolve_school_codes(school_name)
    log('학교 코드 확인 완료')

    try:
        current = fetch_week(school_name, local_code, school_code, 0)
    except RuntimeError as error:
        fail(f'이번 주 시간표를 가져오지 못했습니다: {error}')
    current_week, current_count = build_week(current, grade, cls, this_monday, 'current')
    next_week, next_count = build_week(current, grade, cls, this_monday + dt.timedelta(days=7), 'next')

    try:
        following = fetch_week(school_name, local_code, school_code, 1)
        next_week, next_count = build_week(following, grade, cls, this_monday + dt.timedelta(days=7), 'next')
    except (RuntimeError, SystemExit) as error:
        log(f'다음 주 시간표를 가져오지 못했습니다 (이번 주만 저장): {error}')

    if current_count == 0:
        fail('이번 주 시간표가 비어 있어 저장하지 않았습니다 (빈 값으로 덮어쓰지 않음).')

    payload = {
        'v': 1,
        'source': 'comcigan',
        'synced_at': dt.datetime.now(KST).isoformat(timespec='seconds'),
        'update_date': str(getattr(current, 'update_date', '') or '').strip(),
        'day_labels': DAY_LABELS,
        'period_times': parse_period_times(getattr(current, 'day_time', None)),
        'weeks': [current_week, next_week],
    }

    upsert(payload)

    changed = sum(
        1
        for week in payload['weeks']
        for day in week['days']
        for row in day['periods']
        if row.get('c')
    )
    log(
        '동기화 완료 · 이번 주 %d교시, 다음 주 %d교시, 변동 %d건, 컴시간 갱신 %s, 저장시각 %s'
        % (current_count, next_count, changed, payload['update_date'] or '정보 없음', payload['synced_at'])
    )


if __name__ == '__main__':
    main()
