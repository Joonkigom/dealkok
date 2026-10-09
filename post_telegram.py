"""딜콕 텔레그램 자동 발행

구글 시트(상품 탭)를 읽어서
  - 처음 보는 상품
  - 지난번 올린 가격보다 더 내려간 상품
을 텔레그램 채널에 올리고, 올린 기록을 data/posted.json 에 남긴다.

필요한 GitHub Secrets
  TELEGRAM_BOT_TOKEN : BotFather 가 준 봇 토큰
  TELEGRAM_CHANNEL   : @채널아이디  (예: @dealkok)
"""
import csv
import html
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

SHEET_ID = os.environ.get("SHEET_ID", "1yQWZrqpCm2Vp6C95BpKVNYDVI9msB_VLSAUjB6EGmYc")
SHEET_GID = os.environ.get("SHEET_GID", "0")
SITE_URL = os.environ.get("SITE_URL", "https://joonkigom.github.io/dealkok/")
STATE_PATH = os.environ.get("STATE_PATH", "data/posted.json")
MAX_POSTS_PER_RUN = int(os.environ.get("MAX_POSTS_PER_RUN", "5"))
QUIET_START, QUIET_END = 23, 7  # 한국시간 23시~07시는 발행 쉼 (다음 실행 때 올림)

KST = timezone(timedelta(hours=9))
DISCLOSURE = "이 게시물은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다."


# ---------- 시트 ----------
def fetch_sheet():
    url = (f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq"
           f"?tqx=out:csv&headers=1&gid={SHEET_GID}")
    with urllib.request.urlopen(url, timeout=30) as r:
        text = r.read().decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def num(v):
    digits = "".join(ch for ch in str(v or "") if ch.isdigit() or ch == ".")
    try:
        return int(float(digits)) if digits else 0
    except ValueError:
        return 0


def normalize(r):
    r = {(k or "").strip(): (v or "").strip() for k, v in r.items()}
    now = num(r.get("현재가"))
    hist = [num(x) for x in r.get("가격이력", "").replace("|", ",").split(",")]
    hist = [h for h in hist if h]
    if not hist or hist[-1] != now:
        hist.append(now)
    was = num(r.get("평소가")) or max(hist)
    qty = num(r.get("수량"))
    return {
        "name": r.get("상품명", ""),
        "cat": r.get("카테고리", "") or "기타",
        "now": now,
        "was": was,
        "qty": qty,
        "unit": r.get("단위", "") or "개",
        "img": r.get("이미지", ""),
        "link": r.get("링크", ""),
        "point": r.get("추천포인트", ""),
        "show": (r.get("노출", "Y") or "Y").upper() != "N",
        "pct": round((1 - now / was) * 100) if was > now else 0,
        "is_low": len(hist) > 2 and now <= min(hist),
    }


def key_of(it):
    return it["link"] or it["name"]


# ---------- 메시지 ----------
def won(n):
    return f"{n:,}원"


def build_text(it, reason):
    e = html.escape
    head = []
    if it["is_low"]:
        head.append("🔻 역대 최저가")
    elif reason == "drop":
        head.append("📉 가격 추가 하락")
    else:
        head.append("✨ 새로운 콕")
    if it["pct"]:
        head.append(f"{it['pct']}% 할인")
    lines = [f"<b>{' · '.join(head)}</b>", "", f"<b>{e(it['name'])}</b>"]
    price = f"💰 <b>{won(it['now'])}</b>"
    if it["pct"]:
        price += f"  <s>{won(it['was'])}</s>"
    lines.append(price)
    if it["qty"] > 1:
        lines.append(f"({e(it['unit'])}당 {won(round(it['now'] / it['qty']))})")
    if it["point"]:
        lines += ["", f"💡 {e(it['point'])}"]
    lines += ["", f"<i>가격은 확인 시점 기준이며 바뀔 수 있어요.</i>", f"<i>{DISCLOSURE}</i>"]
    return "\n".join(lines)


def build_buttons(it):
    rows = []
    if it["link"].startswith("http"):
        rows.append([{"text": "🛒 쿠팡에서 지금 가격 보기", "url": it["link"]}])
    rows.append([{"text": "📊 딜콕에서 다른 딜 보기", "url": SITE_URL}])
    return {"inline_keyboard": rows}


# ---------- 텔레그램 ----------
def tg(method, payload):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    data = urllib.parse.urlencode(
        {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, dict) else v) for k, v in payload.items()}
    ).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=30) as r:
        res = json.loads(r.read().decode())
    if not res.get("ok"):
        raise RuntimeError(res)
    return res


def send(it, reason):
    chat = os.environ["TELEGRAM_CHANNEL"]
    text = build_text(it, reason)
    markup = build_buttons(it)
    if it["img"].startswith("http"):
        try:
            return tg("sendPhoto", {"chat_id": chat, "photo": it["img"], "caption": text,
                                   "parse_mode": "HTML", "reply_markup": markup})
        except Exception as e:  # 이미지 실패하면 글로만
            print("사진 발송 실패, 글로 대체:", e)
    return tg("sendMessage", {"chat_id": chat, "text": text, "parse_mode": "HTML",
                              "disable_web_page_preview": "true", "reply_markup": markup})


# ---------- 상태 ----------
def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def pick(items, state):
    out = []
    for it in items:
        if not (it["show"] and it["name"] and it["now"]):
            continue
        prev = state.get(key_of(it))
        if prev is None:
            out.append((it, "new"))
        elif it["now"] < prev.get("price", 0):
            out.append((it, "drop"))
    # 할인율 큰 것부터
    out.sort(key=lambda x: (-x[0]["is_low"], -x[0]["pct"]))
    return out


def main():
    hour = datetime.now(KST).hour
    if os.environ.get("FORCE") != "1" and (hour >= QUIET_START or hour < QUIET_END):
        print(f"조용한 시간({hour}시)이라 발행을 쉽니다.")
        return 0

    items = [normalize(r) for r in fetch_sheet()]
    state = load_state()
    todo = pick(items, state)
    print(f"시트 상품 {len(items)}개, 발행 대상 {len(todo)}개")

    posted = 0
    for it, reason in todo[:MAX_POSTS_PER_RUN]:
        try:
            send(it, reason)
        except Exception as e:
            print("발행 실패:", it["name"], e)
            continue
        state[key_of(it)] = {"name": it["name"], "price": it["now"],
                             "posted_at": datetime.now(KST).isoformat(timespec="minutes")}
        posted += 1
        print(f"발행: [{reason}] {it['name']} {won(it['now'])}")
        time.sleep(3)

    # 가격이 다시 오른 상품은 기준가를 올려서, 다음 하락 때 다시 알림
    for it in items:
        k = key_of(it)
        if k in state and it["now"] > state[k]["price"]:
            state[k]["price"] = it["now"]

    save_state(state)
    print(f"완료: {posted}개 발행")
    return 0


if __name__ == "__main__":
    sys.exit(main())
