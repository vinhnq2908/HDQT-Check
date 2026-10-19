import csv
import io
import os
import ipaddress
import json
import re
import socket
import subprocess
import sys
import unicodedata
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import streamlit as st
import pymupdf

REPORT_TIMEOUT_SECONDS = 900
OCR_DPI = 150
MAX_OCR_PIXELS = 12_000_000

DEFAULT_ERP_TERMS = """ERP
hệ thống ERP
phần mềm ERP
enterprise resource planning
SAP ERP
SAP S/4HANA
Oracle ERP
Oracle Fusion
Microsoft Dynamics
Odoo
NetSuite
Infor ERP
MISA
FAST Accounting
BRAVO"""

DEFAULT_ACCOUNTING_TECH_TERMS = """phần mềm kế toán
hệ thống kế toán
hệ thống thông tin kế toán
kế toán điện tử
accounting software
accounting information system
cloud accounting
hóa đơn điện tử
electronic invoice
e-invoice
chữ ký số
tự động hóa kế toán
accounting automation
RPA
robotic process automation
trí tuệ nhân tạo
artificial intelligence
machine learning
OCR
nhận dạng ký tự quang học
phân tích dữ liệu
data analytics
business intelligence
XBRL
blockchain"""

DEFAULT_ACCOUNTING_CONTEXT = """kế toán
phòng kế toán
bộ phận kế toán
tài chính kế toán
financial accounting
finance and accounting
báo cáo tài chính
financial reporting
sổ cái
general ledger
công nợ phải thu
công nợ phải trả
accounts receivable
accounts payable
đối soát
thuế
tax
hóa đơn
invoice
kiểm toán
audit"""

DEFAULT_ACTION_TERMS = """triển khai
đã triển khai
đang triển khai
áp dụng
đã áp dụng
sử dụng
đang sử dụng
vận hành
đưa vào sử dụng
tích hợp
kết nối
nâng cấp
thay thế
chuyển sang
đầu tư
tự động hóa
số hóa
implemented
deployed
adopted
used
in operation
go-live
integrated
upgraded
automated
digitized"""

DEFAULT_OPERATIONAL_TERMS = """đã triển khai
đã áp dụng
đang sử dụng
đưa vào sử dụng
đang vận hành
vận hành
go-live
in operation
implemented
deployed
adopted
used"""
DEFAULT_PLANNED_TERMS = """dự kiến
kế hoạch
sẽ triển khai
sẽ áp dụng
sẽ sử dụng
đang xem xét
planned
will implement
will deploy
to be implemented"""
DEFAULT_NEGATIVE_TERMS = """chưa triển khai
chưa áp dụng
chưa sử dụng
chưa đưa vào sử dụng
không triển khai
không áp dụng
không sử dụng
not implemented
not deployed
not in use
not adopted"""
ACCOUNTING_SPECIFIC_MARKERS = (
    "ke toan", "accounting", "hoa don", "invoice", "so cai", "general ledger",
    "accounts payable", "accounts receivable", "xbrl", "thue", "tax",
)


def public_url(raw_url: str) -> str:
    url = raw_url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL phải bắt đầu bằng http:// hoặc https://")
    host = parsed.hostname
    try:
        addresses = socket.getaddrinfo(host, None)
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if not ip.is_global:
                raise ValueError("URL trỏ tới địa chỉ mạng nội bộ, bị từ chối")
    except socket.gaierror as exc:
        raise ValueError(f"Không phân giải được tên miền: {host}") from exc
    return url


def download_pdf(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "AnnualReportTechAnalyzer/1.0"})
    with urlopen(request, timeout=30) as response:
        content_type = (response.headers.get("Content-Type") or "").lower()
        data = response.read(100 * 1024 * 1024 + 1)
    if len(data) > 100 * 1024 * 1024:
        raise ValueError("PDF vượt giới hạn 100 MB")
    if "pdf" not in content_type and not data.startswith(b"%PDF"):
        raise ValueError("URL không trả về file PDF")
    return data


def clean_pdf_text(text: str) -> str:
    # ponytail: decode common PDF font CID/uni glyph escapes; add full CMap parsing if binary stream fonts break
    text = re.sub(r"(?:/|\\)uni([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), text)
    text = re.sub(r"(?:/|\\)u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), text)
    text = re.sub(r"(?<!\w)uni([0-9a-fA-F]{4})(?!\w)", lambda m: chr(int(m.group(1), 16)), text)
    return unicodedata.normalize("NFC", text)


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFD", clean_pdf_text(value).casefold())
    value = "".join(char for char in value if unicodedata.category(char) != "Mn")
    return re.sub(r"\s+", " ", value.replace("đ", "d")).strip()


def parse_terms(raw: str) -> list[str]:
    terms, seen = [], set()
    for line in raw.splitlines():
        for value in re.split(r"[,;]", line):
            value = re.sub(r"\s+", " ", value).strip()
            key = normalize_text(value)
            if value and key and key not in seen:
                seen.add(key)
                terms.append(value)
    return terms


def term_pattern(term: str) -> re.Pattern:
    return re.compile(r"(?<!\w)" + re.escape(normalize_text(term)) + r"(?!\w)")


def find_matches(text: str, terms: list[str]) -> list[tuple[int, int, str]]:
    matches = []
    for term in terms:
        matches.extend((m.start(), m.end(), term) for m in term_pattern(term).finditer(text))
    chosen = []
    for start, end, term in sorted(matches, key=lambda item: (item[0], -(item[1] - item[0]))):
        if any(start < old_end and end > old_start for old_start, old_end, _ in chosen):
            continue
        chosen.append((start, end, term))
    return chosen


def count_matches(text: str, terms: list[str]) -> int:
    return len(find_matches(text, terms))


def ocr_tessdata() -> str:
    # ponytail: search project folders first, then venv/system prefixes, upgrade if custom tesseract config needed.
    candidates = (
        Path(__file__).resolve().parent / "tessdata",
        Path(__file__).resolve().parent / ".venv" / "share" / "tessdata",
        Path.cwd() / "tessdata",
        Path(__file__).resolve().parent.parent / "tessdata",
        Path(__file__).resolve().parent.parent.parent / "tessdata",
        Path(sys.prefix) / "share" / "tessdata",
        Path(os.environ.get("TESSDATA_PREFIX", "")),
        Path("C:/Program Files/Tesseract-OCR/tessdata"),
        Path("C:/Program Files (x86)/Tesseract-OCR/tessdata"),
    )
    for folder in candidates:
        try:
            if folder and folder.is_dir() and all((folder / f"{lang}.traineddata").is_file() for lang in ("vie", "eng")):
                return str(folder)
        except Exception:
            pass
    try:
        folder = Path(pymupdf.get_tessdata())
        if all((folder / f"{lang}.traineddata").is_file() for lang in ("vie", "eng")):
            return str(folder)
    except Exception:
        pass
    raise RuntimeError(
        "OCR thiếu vie.traineddata hoặc eng.traineddata. "
        "Đặt cả hai trong thư mục tessdata cạnh app.py, "
        "hoặc cài ngôn ngữ Tesseract theo README.md."
    )

def extract_pages(pdf: bytes) -> tuple[int, list[dict], list[dict]]:
    pages, skipped = [], []
    tessdata, ocr_setup_error = None, ""
    with pymupdf.open(stream=pdf, filetype="pdf") as document:
        if document.needs_pass:
            raise ValueError("PDF có mật khẩu; cần bản không khóa để phân tích.")
        page_count = len(document)
        for index in range(page_count):
            page, text, reason, method = None, "", "", "text"
            try:
                page = document.load_page(index)
                raw = page.get_text(
                    "text", sort=True,
                    flags=pymupdf.TEXTFLAGS_TEXT & ~pymupdf.TEXT_USE_CID_FOR_UNKNOWN_UNICODE,
                )
                text = " ".join(clean_pdf_text(re.sub(r"-\s*\n\s*", "", raw)).split())
            except Exception as exc:
                reason = f"Đọc text lỗi: {type(exc).__name__}: {exc}"

            # ponytail: OCR only low-text/garbled pages; extend for mixed image/text pages if needed.
            if sum(char.isalnum() for char in text) < 20 or text.count("\ufffd") > len(text) * 0.02:
                try:
                    if page is None:
                        raise ValueError("Không mở được trang để OCR.")
                    if page.rect.width * page.rect.height * (OCR_DPI / 72) ** 2 > MAX_OCR_PIXELS:
                        raise ValueError("Trang quá lớn để OCR an toàn ở 150 DPI (quá 12 triệu pixel).")
                    if tessdata is None and not ocr_setup_error:
                        try:
                            tessdata = ocr_tessdata()
                        except Exception as exc:
                            ocr_setup_error = str(exc)
                    if ocr_setup_error:
                        raise RuntimeError(ocr_setup_error)
                    textpage = page.get_textpage_ocr(
                        language="vie+eng", dpi=OCR_DPI, full=True, tessdata=tessdata,
                    )
                    raw = page.get_text("text", textpage=textpage, sort=True)
                    ocr_text = " ".join(clean_pdf_text(re.sub(r"-\s*\n\s*", "", raw)).split())
                    if not ocr_text:
                        raise ValueError("Không thấy chữ sau OCR; trang có thể trống hoặc không đọc được.")
                    if len(ocr_text) >= len(text) or "\ufffd" in text:
                        text, method = ocr_text, "ocr"
                    reason = ""
                except Exception as exc:
                    reason = "; ".join(filter(None, (reason, f"OCR lỗi: {type(exc).__name__}: {exc}")))

            if text and text.count("\ufffd") > len(text) * 0.02:
                reason = "; ".join(filter(None, (reason, "Text còn quá nhiều ký tự không đọc được.")))
                text = ""
            if text:
                pages.append({
                    "page": index + 1, "text": text,
                    "normalized": normalize_text(text), "method": method,
                })
            if reason:
                skipped.append({"page": index + 1, "reason": reason, "retained_text": bool(text)})
    if not pages:
        raise ValueError(
            "Không đọc được trang có chữ; không chấm mức áp dụng. "
            + "; ".join(f"Trang {item['page']}: {item['reason']}" for item in skipped[:10])
        )
    return page_count, pages, skipped


LEVELS = {
    0: "0 - Không có bằng chứng áp dụng",
    1: "1 - Chỉ đề cập",
    2: "2 - Có kế hoạch/dự kiến",
    3: "3 - Đang triển khai/áp dụng",
    4: "4 - Đã áp dụng/đang vận hành",
}
MIN_EVIDENCE = 3


def classify_evidence(name, url, pages_count, pages, erp_terms, tech_terms, context_terms,
                      action_terms, operational_terms, planned_terms, negative_terms,
                      min_evidence=MIN_EVIDENCE) -> dict:
    raw_erp = raw_tech = relevant_erp = relevant_tech = context_hits = 0
    action_hits = operational_hits = planned_hits = negative_hits = word_count = 0
    s1 = s2 = s3 = s4 = points = best_score = 0
    relevant_terms, evidence, stuck = set(), [], []
    for page in pages:
        word_count += len(page["text"].split())
        raw_erp += count_matches(page["normalized"], erp_terms)
        raw_tech += count_matches(page["normalized"], tech_terms)
        for unit in re.split(r"(?<=[.!?。！？])\s+", page["text"]):
            unit = clean_pdf_text(unit)
            normalized = normalize_text(unit)
            erp = find_matches(normalized, erp_terms)
            tech = find_matches(normalized, tech_terms)
            context = find_matches(normalized, context_terms)
            if not erp and not tech:
                continue
            s1 += 1
            is_erp_relevant = bool(erp and context)
            is_tech_relevant = bool(tech and (context or any(
                any(marker in normalize_text(term) for marker in ACCOUNTING_SPECIFIC_MARKERS)
                for _, _, term in tech
            )))
            if not (is_erp_relevant or is_tech_relevant):
                labels = "; ".join(term for _, _, term in erp + tech)
                stuck.append(f"Kẹt sàng 2 (thiếu ngữ cảnh kế toán) | Trang {page['page']} | {labels} | {unit[:500]}")
                continue
            s2 += 1
            relevant = (erp if is_erp_relevant else []) + (tech if is_tech_relevant else [])
            relevant_terms.update(term for _, _, term in relevant)
            relevant_erp += len(erp) if is_erp_relevant else 0
            relevant_tech += len(tech) if is_tech_relevant else 0
            context_hits += len(context)
            actions = count_matches(normalized, action_terms)
            operational = count_matches(normalized, operational_terms)
            planned = count_matches(normalized, planned_terms)
            negative = count_matches(normalized, negative_terms)
            action_hits += actions
            operational_hits += operational
            planned_hits += planned
            negative_hits += negative
            if negative:
                status, score = "Phủ định/không áp dụng", 0
            elif planned:
                status, score = LEVELS[2], 2
            elif operational:
                status, score = LEVELS[4], 4
            elif actions:
                status, score = LEVELS[3], 3
            else:
                status, score = LEVELS[1], 1
            s3 += score >= 2
            s4 += score == 4
            points += score
            best_score = max(best_score, score)
            labels = "; ".join(term for _, _, term in relevant)
            evidence.append(f"Trang {page['page']} | {status} | {labels} | {unit[:700]}")
            if score <= 1:
                stuck.append(f"Kẹt sàng 3 ({status}) | Trang {page['page']} | {labels} | {unit[:500]}")

    if s1 == 0:
        stuck_stage = "Sàng 1: không có từ khóa ERP/công nghệ"
    elif s2 == 0:
        stuck_stage = "Sàng 2: có từ khóa nhưng không có ngữ cảnh kế toán"
    elif s3 == 0:
        stuck_stage = "Sàng 3: có ngữ cảnh kế toán nhưng chưa có hành động/kế hoạch"
    elif s4 == 0:
        stuck_stage = "Sàng 4: có triển khai/kế hoạch nhưng chưa có bằng chứng đã vận hành"
    else:
        stuck_stage = "Qua đủ 4 sàng"

    if not evidence:
        evidence_status = "Không có bằng chứng"
    elif len(evidence) < min_evidence:
        evidence_status = "Ít bằng chứng"
    else:
        evidence_status = "Đủ bằng chứng"

    stuck_evidence = "\n".join(stuck[:15])
    if evidence_status != "Đủ bằng chứng" and not stuck_evidence:
        stuck_evidence = f"{stuck_stage} | báo cáo không có câu nào để hiển thị"
    if evidence_status == "Đủ bằng chứng":
        stuck_evidence = ""

    return {
        "document": name, "url": url, "pages": pages_count, "word_count": word_count,
        "adoption_level": best_score, "adoption_level_name": LEVELS[best_score],
        "adoption_points": points,
        "evidence_status": evidence_status, "evidence_count": len(evidence),
        "stuck_stage": stuck_stage,
        "stage1_keyword_sentences": s1, "stage2_accounting_sentences": s2,
        "stage3_action_or_plan_sentences": s3, "stage4_operational_sentences": s4,
        "erp_frequency": raw_erp, "erp_accounting_frequency": relevant_erp,
        "accounting_tech_frequency": raw_tech,
        "accounting_tech_relevant_frequency": relevant_tech,
        "technology_frequency": relevant_erp + relevant_tech,
        "technology_diversity": len(relevant_terms), "accounting_context_hits": context_hits,
        "action_word_hits": action_hits, "operational_evidence_hits": operational_hits,
        "planned_evidence_hits": planned_hits, "negative_evidence_hits": negative_hits,
        "relevant_terms": "; ".join(sorted(relevant_terms)),
        "evidence": "\n".join(evidence[:20]),
        "stuck_evidence": stuck_evidence,
    }


def rank_rows(rows: list[dict]) -> None:
    keys = sorted({(r["adoption_level"], r["adoption_points"]) for r in rows}, reverse=True)
    for row in rows:
        row["adoption_rank"] = keys.index((row["adoption_level"], row["adoption_points"])) + 1


def report_worker(request_path: str, result_path: str) -> None:
    try:
        request = json.loads(Path(request_path).read_text(encoding="utf-8"))
        url = public_url(request["url"])
        page_count, pages, issues = extract_pages(download_pdf(url))
        skipped = [item for item in issues if not item["retained_text"]]
        ocr_pages = [str(page["page"]) for page in pages if page["method"] == "ocr"]
        row = classify_evidence(
            url.rsplit("/", 1)[-1] or url, url, page_count, pages, *request["groups"],
        )
        row.update({
            "extraction_status": "partial" if issues else "completed",
            "pages_with_text": len(pages),
            "ocr_page_count": len(ocr_pages),
            "ocr_pages": "; ".join(ocr_pages),
            "incomplete_page_count": len(issues),
            "skipped_page_count": len(skipped),
            "skipped_pages": "; ".join(str(item["page"]) for item in skipped),
            "extraction_errors": "\n".join(
                f"Trang {item['page']}: {item['reason']}"
                + (" (giữ phần text đọc được)" if item["retained_text"] else "")
                for item in issues
            ),
        })
    except Exception as exc:
        row = {"error": f"{type(exc).__name__}: {exc}"}
    Path(result_path).write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")


def run_report(url: str, groups: list[list[str]], timeout_seconds: float = REPORT_TIMEOUT_SECONDS) -> dict:
    with TemporaryDirectory(prefix="annual-report-") as folder:
        request_path = Path(folder) / "request.json"
        result_path = Path(folder) / "result.json"
        request_path.write_text(
            json.dumps({"url": url, "groups": groups}, ensure_ascii=False), encoding="utf-8",
        )
        try:
            subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--analyze-report",
                 str(request_path), str(result_path)],
                timeout=timeout_seconds, check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                f"Quá {timeout_seconds:g} giây/file; đã dừng xử lý và chuyển file tiếp theo."
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"Tiến trình đọc PDF dừng bất thường (mã {exc.returncode}); không chấm điểm."
            ) from exc
        row = json.loads(result_path.read_text(encoding="utf-8"))
        if "error" in row:
            raise ValueError(row["error"])
        return row


def main() -> None:
    st.set_page_config(page_title="ERP và Công nghệ Kế toán", layout="wide")
    st.title("Phân tích ERP và công nghệ trong kế toán")
    st.caption("Đếm từ khóa theo ngữ cảnh câu, chấm mức áp dụng, giữ số trang PDF và xuất CSV.")
    st.caption(
        f"Đọc text trước; OCR tiếng Việt/Anh khi trang có ít chữ hoặc lỗi ký tự. "
        f"Tối đa {REPORT_TIMEOUT_SECONDS} giây/file, gồm tải và phân tích; xử lý tuần tự."
    )

    with st.sidebar:
        st.header("Bộ từ khóa")
        erp_input = st.text_area("Từ khóa ERP", value=DEFAULT_ERP_TERMS, height=230)
        tech_input = st.text_area("Công nghệ kế toán", value=DEFAULT_ACCOUNTING_TECH_TERMS, height=300)
        context_input = st.text_area("Ngữ cảnh kế toán", value=DEFAULT_ACCOUNTING_CONTEXT, height=250)
        action_input = st.text_area("Từ hành động", value=DEFAULT_ACTION_TERMS, height=220)
        operational_input = st.text_area("Từ cho thấy đã vận hành", value=DEFAULT_OPERATIONAL_TERMS, height=170)
        planned_input = st.text_area("Từ cho thấy kế hoạch", value=DEFAULT_PLANNED_TERMS, height=170)
        negative_input = st.text_area("Cụm phủ định", value=DEFAULT_NEGATIVE_TERMS, height=170)
        st.info("ERP/công nghệ chỉ tính liên quan kế toán khi cùng câu có ngữ cảnh kế toán. Câu phủ định không tính là đã áp dụng.")

    links = st.text_area("Link báo cáo PDF", placeholder="Mỗi dòng một URL PDF công khai", height=160)

    if st.button("Phân tích", type="primary"):
        groups = [parse_terms(value) for value in (
            erp_input, tech_input, context_input, action_input,
            operational_input, planned_input, negative_input,
        )]
        erp_terms, tech_terms, context_terms, action_terms, operational_terms, planned_terms, negative_terms = groups
        urls = [line.strip() for line in links.splitlines() if line.strip()]
        if not erp_terms and not tech_terms:
            st.error("Cần ít nhất một từ khóa ERP hoặc công nghệ kế toán.")
        elif not urls:
            st.error("Cần ít nhất một link PDF.")
        else:
            # ponytail: session-only results; use durable storage for reload/restart recovery.
            rows = st.session_state["analysis_rows"] = []
            failures = st.session_state["analysis_failures"] = []
            progress = st.progress(0)
            current = st.empty()
            for index, raw_url in enumerate(urls, start=1):
                current.info(
                    f"Đang xử lý {index}/{len(urls)} — "
                    f"tối đa {REPORT_TIMEOUT_SECONDS} giây/file: {raw_url}"
                )
                try:
                    row = run_report(raw_url, groups)
                    rows.append(row)
                    if row["extraction_status"] == "partial":
                        st.warning(
                            f"{row['document']}: {row['incomplete_page_count']} trang chưa đọc đầy đủ, "
                            f"bỏ qua {row['skipped_page_count']} trang "
                            f"({row['skipped_pages'] or 'không có'}). "
                            "Điểm tạm thời chỉ dựa trên phần đọc được; xem extraction_errors."
                        )
                    else:
                        st.success(f"Đã phân tích: {row['document']} — OCR {row['ocr_page_count']} trang.")
                except Exception as exc:
                    failures.append({
                        "url": raw_url,
                        "status": "timeout" if isinstance(exc, TimeoutError) else "error",
                        "error": str(exc),
                    })
                    st.warning(f"Bỏ qua {raw_url}: {exc}")
                progress.progress(index / len(urls))
            current.empty()

    rows = st.session_state.get("analysis_rows", [])
    failures = st.session_state.get("analysis_failures", [])
    if failures:
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=["url", "status", "error"])
        writer.writeheader()
        writer.writerows(failures)
        st.download_button(
            "Tải danh sách file lỗi/quá thời gian",
            output.getvalue().encode("utf-8-sig"),
            "failed_reports.csv", "text/csv",
        )

    if rows:
        rank_rows(rows)
        st.subheader("Kết quả lần phân tích gần nhất")
        st.dataframe(rows, use_container_width=True)
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
        st.download_button(
            "Tải kết quả CSV",
            output.getvalue().encode("utf-8-sig"),
            "erp_accounting_technology_analysis.csv",
            "text/csv",
        )


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--analyze-report":
        report_worker(sys.argv[2], sys.argv[3])
    else:
        main()
