import csv
import io
import json
import re
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import streamlit as st

from app import REPORT_TIMEOUT_SECONDS, download_pdf, extract_pages, normalize_text, public_url

# Chức danh khớp trên text đã bỏ dấu. Loại Ban kiểm soát / Ủy ban / Hội đồng thành viên.
_NOT = r"(?! (?:ban|uy ban|tieu ban|cong doan|hoi dong (?!quan tri))\b)"
_B = r"(?: hdqt| hoi dong quan tri| board of directors)?"
POS = re.compile(
    r"\b(?:"
    r"(?P<deputy>pho tong giam doc|pho giam doc|deputy general director|deputy ceo)"
    r"|(?P<vice>(?:pho chu tich|vice chair(?:man|woman|person)|deputy chair(?:man|woman|person))" + _B + _NOT + r")"
    r"|(?P<chair>(?:chu tich|chair(?:man|woman|person))" + _B + _NOT + r")"
    r"|(?P<member>(?:thanh vien|member of (?:the )?board(?: of directors)?"
    r"|(?:independent |non-executive |executive )director)"
    r"(?: doc lap| dieu hanh| khong dieu hanh| hdqt| hoi dong quan tri)*" + _NOT + r")"
    r"|(?P<ceo>tong giam doc|giam doc dieu hanh|general director|chief executive officer|ceo)"
    r")\b"
)
# Ông/Bà/Mr/Ms + tối đa 6 từ; lookahead để không nuốt tiền tố của người kế tiếp.
NAME = re.compile(r"(?<!\w)(?:ông|bà|mrs|mr|ms)(?!\w)\.?\s+(?=((?:[^\W\d_]+ +){0,5}[^\W\d_]+))", re.I)
STOP = {"giam", "pho", "tong", "hdqt", "kiem", "ceo", "uy", "hoi", "truong"}
HONORIFICS = {"ong", "ba", "mr", "mrs", "ms", "dr", "ts", "ths", "gs", "pgs", "prof"}
BOARD = {"chair", "vice", "member"}
FORMER = re.compile(r"mien nhiem|tu nhiem|thoi giu|het nhiem ky|resign|ceased|dismiss")
STATED = re.compile(
    r"(?:hdqt|hoi dong quan tri|board of directors)[^.]{0,60}?\b(?:gom|co|la|consists? of|comprises?)\s+"
    r"(?:[a-z]+\s+)?\(?(\d{1,2})\)?\s+(?:thanh vien|members|directors)"
)
SUBS = re.compile(
    r"(?:\b(co|gom|bao gom|tong so|hien co|so luong)\s+)?(?<![\d.,%])(\d{1,3})\s+"
    r"(?:cong ty con|subsidiar(?:y|ies))"
)


def name_key(name: str) -> tuple:
    words = re.findall(r"[a-z]+", normalize_text(name))
    while words and words[0] in HONORIFICS:
        words.pop(0)
    return tuple(sorted(words))  # sort: "Nguyễn Văn A" = "A Nguyen Van"


def chair_is_ceo(chair: str, ceo: str):
    """True/False; None khi thiếu tên (thiếu không được coi là 'khác')."""
    a, b = name_key(chair or ""), name_key(ceo or "")
    return (a == b) if a and b else None


def label(match) -> tuple:
    kind, text = match.lastgroup, match.group(match.lastgroup)
    if kind == "member":
        if "doc lap" in text or "independent" in text:
            return kind, "Thành viên HĐQT độc lập"
        if "khong dieu hanh" in text or "non-executive" in text:
            return kind, "Thành viên HĐQT không điều hành"
        if "dieu hanh" in text or "executive" in text:
            return kind, "Thành viên HĐQT điều hành"
        return kind, "Thành viên HĐQT"
    return kind, {"chair": "Chủ tịch HĐQT", "vice": "Phó chủ tịch HĐQT", "ceo": "Tổng giám đốc"}[kind]


def lead(norm: str) -> list:
    """Chức danh ngay SAU tên: 'Ông A – Chủ tịch HĐQT kiêm Tổng giám đốc'."""
    out, pos = [], 0
    for m in POS.finditer(norm):
        if m.start() - pos > 12 or m.lastgroup == "deputy":
            break
        out.append(label(m))
        pos = m.end()
    return out


def trail(norm: str) -> list:
    """Chức danh ngay TRƯỚC tên: 'Chủ tịch HĐQT: Ông A'."""
    out, end = [], len(norm)
    for m in reversed(list(POS.finditer(norm))):
        if end - m.end() > (12 if out else 8) or m.lastgroup == "deputy":
            break
        out.append(label(m))
        end = m.start()
    return out[::-1]


def clean_name(raw: str) -> list:
    words, out = raw.split(), []
    for i, word in enumerate(words):
        if not word[0].isupper() or normalize_text(word) in STOP or POS.match(normalize_text(" ".join(words[i:]))):
            break
        out.append(word)
    return out


def parse_people(text: str) -> list:
    # ponytail: chỉ nhận tên có tiền tố Ông/Bà/Mr/Ms kèm 1 chức danh liền kề; bảng không tiền tố hoặc
    # 'HĐQT gồm: Ông A, Ông B' sẽ bị sót -> thêm parser riêng khi báo cáo thực tế cần.
    marks = list(NAME.finditer(text))
    people = {}
    for i, m in enumerate(marks):
        words = clean_name(m.group(1))
        if len(words) < 2:
            continue
        name = " ".join(words)
        end = m.start(1) + len(name)
        nxt = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        before = text[max(marks[i - 1].end() if i else 0, m.start() - 70):m.start()]
        after = normalize_text(re.split(r"\.\s", text[end:min(nxt, end + 140)], 1)[0])
        if FORMER.search(after):
            continue
        found = (trail(normalize_text(before)) if before.rstrip().endswith(":") else []) or lead(after)
        if not found:
            continue
        person = people.setdefault(name_key(name), {
            "key": name_key(name), "name": name.title() if name.isupper() else name,
            "kinds": set(), "labels": [], "snippet": text[max(0, m.start() - 40):end + 110],
        })
        for kind, title in found:
            person["kinds"].add(kind)
            if title not in person["labels"]:
                person["labels"].append(title)
    return list(people.values())


def snip(page: dict, start: int, end: int) -> str:
    # Chỉ dùng text gốc khi độ dài khớp bản đã chuẩn hóa (chỉ số còn đúng); không thì dùng bản không dấu.
    src = page["text"] if len(page["text"]) == len(page["normalized"]) else page["normalized"]
    return f"Tr.{page['page']}: {src[max(0, start - 60):end + 80]}"


def analyze_board(pages: list) -> dict:
    people = [(p["page"], x) for p in pages for x in parse_people(p["text"])]
    by_page = {}
    for pg, x in people:
        if x["kinds"] & BOARD:
            by_page.setdefault(pg, []).append(x)
    best = max(by_page, key=lambda pg: len(by_page[pg]), default=None)
    listed = best is not None and len(by_page[best]) >= 2
    # Có trang liệt kê: lấy trang đó ±1 (danh sách vắt qua trang). Không có: ghép trang lẻ, độ tin cậy thấp.
    use = [pg for pg in by_page if abs(pg - best) <= 1] if listed else list(by_page)
    members = {}
    for pg in use:
        for x in by_page[pg]:
            m = members.setdefault(x["key"], {"name": x["name"], "labels": [], "page": pg, "snippet": x["snippet"]})
            m["labels"] += [t for t in x["labels"] if t not in m["labels"]]

    def top(kind):  # người được gắn chức danh ở nhiều trang nhất, ưu tiên trang HĐQT
        found = {}
        for pg, x in people:
            if kind in x["kinds"]:
                found.setdefault(x["key"], []).append((pg, x))
        pick = max(found.values(), key=lambda v: (any(pg in use for pg, _ in v), len(v)), default=None)
        return pick[0] if pick else None

    chair, ceo = top("chair"), top("ceo")
    for hit, title in ((chair, "Chủ tịch HĐQT"), (ceo, "Tổng giám đốc")):
        if hit and hit[1]["key"] in members and title not in members[hit[1]["key"]]["labels"]:
            members[hit[1]["key"]]["labels"].append(title)
    for m in members.values():
        if any(t.startswith(("Chủ tịch", "Phó chủ tịch")) for t in m["labels"]):
            m["labels"] = [t for t in m["labels"] if t != "Thành viên HĐQT"]

    stated, stated_ev, subs, hdqt_pages = None, "", [], []
    for p in pages:
        norm = p["normalized"]
        if "hoi dong quan tri" in norm or "board of directors" in norm:
            hdqt_pages.append(p["page"])
        if stated is None and (m := STATED.search(norm)):
            stated, stated_ev = int(m.group(1)), snip(p, m.start(), m.end())
        subs += [(m.group(1) is not None, int(m.group(2)), snip(p, m.start(), m.end())) for m in SUBS.finditer(norm)]
    preferred = [s for s in subs if s[0]]
    sub = (preferred or subs or [None])[0]

    n, notes = len(members), []
    if not n:
        notes.append("Không đọc được danh sách thành viên HĐQT (cần tiền tố Ông/Bà/Mr/Ms + chức danh); xem hdqt_pages")
    elif not listed:
        notes.append("Thành viên ghép từ các trang lẻ, độ tin cậy thấp")
    if stated is None:
        check = "chưa thấy số khai báo"
    elif stated == n:
        check = "khớp"
    else:
        check = f"lệch (khai báo {stated}, đọc được {n})"
        notes.append("Số thành viên đọc được khác số khai báo; kiểm tra evidence")
    if not chair:
        notes.append("Không thấy Chủ tịch HĐQT")
    if not ceo:
        notes.append("Không thấy Tổng giám đốc")
    if not sub:
        notes.append("Không thấy câu nêu số công ty con (chưa đếm bảng danh sách)")
    elif not sub[0]:
        notes.append("Số công ty con lấy từ câu không có 'có/gồm'; độ tin cậy thấp")
    if len({s[1] for s in preferred}) > 1:
        notes.append("Nhiều số công ty con khác nhau: " + ", ".join(str(s[1]) for s in preferred))

    evidence = [f"{tag} - Tr.{hit[0]}: {hit[1]['snippet']}" for tag, hit in (("Chủ tịch", chair), ("Tổng giám đốc", ceo)) if hit]
    if stated_ev:
        evidence.append(f"Số TV khai báo - {stated_ev}")
    if sub:
        evidence.append(f"Công ty con - {sub[2]}")
    evidence += [f"TV {i} - Tr.{m['page']}: {m['snippet']}" for i, m in enumerate(members.values(), 1)]

    same = chair_is_ceo(chair and chair[1]["name"], ceo and ceo[1]["name"])
    return {
        "board_member_count": n or "",
        "board_count_stated": stated or "",
        "count_check": check,
        "board_members": "; ".join(f"{m['name']} ({', '.join(m['labels'])})" for m in members.values()),
        "chair_name": chair[1]["name"] if chair else "",
        "ceo_name": ceo[1]["name"] if ceo else "",
        "chair_is_ceo": {True: 1, False: 0, None: ""}[same],
        "subsidiary_count": sub[1] if sub else "",
        "board_pages": "; ".join(map(str, sorted(use))),
        "hdqt_pages": "; ".join(map(str, hdqt_pages[:15])),
        "notes": "; ".join(notes),
        "evidence": "\n".join(evidence),
    }


def board_worker(request_path: str, result_path: str) -> None:
    try:
        url = public_url(json.loads(Path(request_path).read_text(encoding="utf-8"))["url"])
        page_count, pages, issues = extract_pages(download_pdf(url))
        row = {
            "document": url.rsplit("/", 1)[-1] or url, "url": url, "page_count": page_count,
            "pages_with_text": len(pages), "ocr_page_count": sum(p["method"] == "ocr" for p in pages),
            "extraction_status": "partial" if issues else "completed", **analyze_board(pages),
        }
        if issues:
            row["notes"] = "; ".join(filter(None, (
                row["notes"], f"{len(issues)} trang chưa đọc đầy đủ: " + ", ".join(str(i["page"]) for i in issues[:20]),
            )))
    except Exception as exc:
        row = {"error": f"{type(exc).__name__}: {exc}"}
    Path(result_path).write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")


def run_board(url: str, timeout_seconds: float = REPORT_TIMEOUT_SECONDS) -> dict:
    with TemporaryDirectory(prefix="board-report-") as folder:
        request_path, result_path = Path(folder) / "request.json", Path(folder) / "result.json"
        request_path.write_text(json.dumps({"url": url}), encoding="utf-8")
        try:
            subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--analyze-board", str(request_path), str(result_path)],
                timeout=timeout_seconds, check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"Quá {timeout_seconds:g} giây/file; đã dừng và chuyển file tiếp theo.") from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"Tiến trình đọc PDF dừng bất thường (mã {exc.returncode}).") from exc
        row = json.loads(result_path.read_text(encoding="utf-8"))
        if "error" in row:
            raise ValueError(row["error"])
        return row


def to_csv(rows: list, fields) -> bytes:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode("utf-8-sig")


def main() -> None:
    st.set_page_config(page_title="Hội đồng quản trị trong BCTN", layout="wide")
    st.title("Hội đồng quản trị và công ty con trong báo cáo thường niên")
    st.caption(
        "Trích thành viên, chức danh HĐQT, so Chủ tịch với Tổng giám đốc, số công ty con. "
        f"Tối đa {REPORT_TIMEOUT_SECONDS} giây/file; xử lý tuần tự, giữ thứ tự link. "
        "Ô trống = không tìm thấy (không phải 0); xem notes và evidence."
    )
    links = st.text_area("Link báo cáo PDF", placeholder="Mỗi dòng một URL PDF công khai", height=160)
    if st.button("Phân tích", type="primary"):
        urls = [line.strip() for line in links.splitlines() if line.strip()]
        if not urls:
            st.error("Cần ít nhất một link PDF.")
        else:
            rows = st.session_state["board_rows"] = []
            failures = st.session_state["board_failures"] = []
            progress, current = st.progress(0), st.empty()
            for index, url in enumerate(urls, start=1):
                current.info(f"Đang xử lý {index}/{len(urls)}: {url}")
                try:
                    rows.append(run_board(url))
                except Exception as exc:
                    failures.append({"url": url, "status": "timeout" if isinstance(exc, TimeoutError) else "error", "error": str(exc)})
                    st.warning(f"Bỏ qua {url}: {exc}")
                progress.progress(index / len(urls))
            current.empty()

    rows, failures = st.session_state.get("board_rows", []), st.session_state.get("board_failures", [])
    if failures:
        st.download_button("Tải danh sách file lỗi", to_csv(failures, ["url", "status", "error"]), "board_failed.csv", "text/csv")
    if rows:
        st.subheader("Kết quả lần phân tích gần nhất")
        st.dataframe(rows)
        st.download_button("Tải kết quả CSV", to_csv(rows, rows[0].keys()), "board_analysis.csv", "text/csv")


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--analyze-board":
        board_worker(sys.argv[2], sys.argv[3])
    else:
        main()