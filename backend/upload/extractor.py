"""
Extracts absent-student register numbers (and any available metadata such as
department / section / date) from an uploaded PDF, Excel, CSV, or TXT file.

Design notes:
- We don't assume a fixed column layout. Excel/CSV files are scanned for a
  column that looks like a register-number column; if none is obviously
  labeled, every cell is scanned with the register-number regex.
- PDFs are parsed with pdfplumber first (tables + text); PyMuPDF (fitz) is
  used as a fallback for image-light PDFs where pdfplumber finds nothing.
- Every path returns the same dict shape so the rest of the app doesn't care
  which file type was uploaded.
"""
import re
import csv
import io
import pandas as pd
import pdfplumber
import fitz  # PyMuPDF

REGISTER_NUMBER_REGEX = r"\b\d{10,15}\b"
NAME_LIKE_COLUMNS = {"name", "student name", "studentname", "student_name"}
REGNO_LIKE_COLUMNS = {
    "register number", "registernumber", "reg no", "reg_no", "regno",
    "roll number", "rollnumber", "roll no", "register_number", "student id",
}
DEPT_LIKE_COLUMNS = {"department", "dept", "branch"}
SECTION_LIKE_COLUMNS = {"section", "sec"}
DATE_LIKE_COLUMNS = {"date", "attendance date"}


def _empty_result():
    return {
        "absentees": [],       # list of {"register_number": str, "name": str|None}
        "department": None,
        "section": None,
        "date": None,
        "total_students_detected": 0,
        "warnings": [],
    }


def _extract_from_dataframe(df: pd.DataFrame) -> dict:
    result = _empty_result()
    cols_lower = {c: str(c).strip().lower() for c in df.columns}

    regno_col = next((c for c, lc in cols_lower.items() if lc in REGNO_LIKE_COLUMNS), None)
    name_col = next((c for c, lc in cols_lower.items() if lc in NAME_LIKE_COLUMNS), None)
    dept_col = next((c for c, lc in cols_lower.items() if lc in DEPT_LIKE_COLUMNS), None)
    section_col = next((c for c, lc in cols_lower.items() if lc in SECTION_LIKE_COLUMNS), None)
    date_col = next((c for c, lc in cols_lower.items() if lc in DATE_LIKE_COLUMNS), None)

    # Optional status column tells us who is actually ABSENT; if missing,
    # we treat every register number found in the file as absent (the file
    # is assumed to be an "absentees list", matching the workflow spec).
    status_col = next(
        (c for c in df.columns if str(c).strip().lower() in {"status", "attendance", "attendance status"}),
        None,
    )

    seen = set()
    if regno_col is not None:
        for _, row in df.iterrows():
            raw = str(row[regno_col]).strip()
            match = re.search(REGISTER_NUMBER_REGEX, raw)
            if not match:
                continue
            regno = match.group(0)

            if status_col is not None:
                status_val = str(row[status_col]).strip().lower()
                if status_val not in {"absent", "a", "ab"}:
                    continue

            if regno in seen:
                continue
            seen.add(regno)
            name = str(row[name_col]).strip() if name_col is not None and pd.notna(row[name_col]) else None
            result["absentees"].append({"register_number": regno, "name": name})
    else:
        # Fallback: scan every cell for register-number-shaped values.
        for _, row in df.iterrows():
            for val in row:
                match = re.search(REGISTER_NUMBER_REGEX, str(val))
                if match and match.group(0) not in seen:
                    seen.add(match.group(0))
                    result["absentees"].append({"register_number": match.group(0), "name": None})
        if not result["absentees"]:
            result["warnings"].append(
                "No labeled register-number column found; scanned all cells."
            )

    if not result["absentees"] and name_col is not None:
        for _, row in df.iterrows():
            raw_name = str(row[name_col]).strip() if pd.notna(row[name_col]) else ""
            if raw_name and raw_name not in seen and len(raw_name) > 1:
                seen.add(raw_name)
                result["absentees"].append({"register_number": None, "name": raw_name})
        if result["absentees"]:
            result["warnings"].append(
                "No register numbers found; extracted student names instead."
            )

    if dept_col is not None and len(df) and pd.notna(df[dept_col].iloc[0]):
        result["department"] = str(df[dept_col].iloc[0]).strip()
    if section_col is not None and len(df) and pd.notna(df[section_col].iloc[0]):
        result["section"] = str(df[section_col].iloc[0]).strip()
    if date_col is not None and len(df) and pd.notna(df[date_col].iloc[0]):
        result["date"] = str(df[date_col].iloc[0]).strip()

    result["total_students_detected"] = len(result["absentees"])
    return result


def extract_from_excel(filepath: str) -> dict:
    df = pd.read_excel(filepath, dtype=str)
    return _extract_from_dataframe(df)


def extract_from_csv(filepath: str) -> dict:
    df = pd.read_csv(filepath, dtype=str)
    return _extract_from_dataframe(df)


def extract_from_text(text: str) -> dict:
    """Extract absentees from raw text (register numbers or names, one per line)."""
    result = _empty_result()
    seen = set()
    for match in re.finditer(REGISTER_NUMBER_REGEX, text):
        if match.group(0) not in seen:
            seen.add(match.group(0))
            result["absentees"].append({"register_number": match.group(0), "name": None})
    if not result["absentees"]:
        for line in text.splitlines():
            name = line.strip().strip(",").strip(";").strip("-")
            if name and name not in seen and len(name) > 1:
                seen.add(name)
                result["absentees"].append({"register_number": None, "name": name})
        if result["absentees"]:
            result["warnings"].append(
                "No register numbers found; extracted student names instead."
            )
    result["total_students_detected"] = len(result["absentees"])
    return result


def extract_from_txt(filepath: str) -> dict:
    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
        text = f.read()
    return extract_from_text(text)


def extract_from_pdf(filepath: str) -> dict:
    result = _empty_result()
    seen = set()

    # Attempt 1: pdfplumber (handles tables well)
    try:
        with pdfplumber.open(filepath) as pdf:
            for page in pdf.pages:
                for table in page.extract_tables() or []:
                    header = [str(h).strip().lower() if h else "" for h in table[0]]
                    if any(h in REGNO_LIKE_COLUMNS for h in header):
                        regno_idx = next(i for i, h in enumerate(header) if h in REGNO_LIKE_COLUMNS)
                        name_idx = next((i for i, h in enumerate(header) if h in NAME_LIKE_COLUMNS), None)
                        for row in table[1:]:
                            if regno_idx >= len(row) or not row[regno_idx]:
                                continue
                            match = re.search(REGISTER_NUMBER_REGEX, str(row[regno_idx]))
                            if match and match.group(0) not in seen:
                                seen.add(match.group(0))
                                name = row[name_idx] if name_idx is not None and name_idx < len(row) else None
                                result["absentees"].append({"register_number": match.group(0), "name": name})
                    else:
                        for row in table:
                            for cell in row:
                                match = re.search(REGISTER_NUMBER_REGEX, str(cell))
                                if match and match.group(0) not in seen:
                                    seen.add(match.group(0))
                                    result["absentees"].append({"register_number": match.group(0), "name": None})

                text = page.extract_text() or ""
                for match in re.finditer(REGISTER_NUMBER_REGEX, text):
                    if match.group(0) not in seen:
                        seen.add(match.group(0))
                        result["absentees"].append({"register_number": match.group(0), "name": None})
    except Exception:
        result["warnings"].append("pdfplumber failed; falling back to PyMuPDF text scan.")

    # Attempt 2: PyMuPDF fallback if nothing found (e.g. scanned/odd PDFs)
    if not result["absentees"]:
        try:
            doc = fitz.open(filepath)
            for page in doc:
                text = page.get_text()
                for match in re.finditer(REGISTER_NUMBER_REGEX, text):
                    if match.group(0) not in seen:
                        seen.add(match.group(0))
                        result["absentees"].append({"register_number": match.group(0), "name": None})
            doc.close()
        except Exception as e:
            result["warnings"].append(f"PyMuPDF fallback also failed: {e}")

    result["total_students_detected"] = len(result["absentees"])
    return result


def extract_absentees(filepath: str, file_ext: str) -> dict:
    """Dispatch to the right extractor based on file extension."""
    file_ext = file_ext.lower().lstrip(".")
    if file_ext in ("xlsx", "xls"):
        return extract_from_excel(filepath)
    if file_ext == "csv":
        return extract_from_csv(filepath)
    if file_ext == "txt":
        return extract_from_txt(filepath)
    if file_ext == "pdf":
        return extract_from_pdf(filepath)
    raise ValueError(f"Unsupported file type: {file_ext}")
