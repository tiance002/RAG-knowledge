"""Excel row preparation; header selection is explicit, never guessed."""
from io import BytesIO
import re

import pandas as pd
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

IMAGE_FUNCTION = re.compile(r'^\s*=\s*(?:_xlfn\.)?(?:DISPIMG|IMAGE)\s*\(', re.I)


def prepare_excel_workbook(source):
    """Match upstream unmerge/fill/save BEFORE pandas reads the workbook.

    The save/read roundtrip affects numeric representation and readable ranges;
    replacing DataFrame values after reading is not equivalent.
    """
    workbook = load_workbook(BytesIO(source), data_only=True)
    changed = False
    try:
        for sheet in workbook.worksheets:
            for area in list(sheet.merged_cells.ranges):
                value = sheet.cell(area.min_row, area.min_col).value
                sheet.unmerge_cells(str(area))
                for row in sheet.iter_rows(min_row=area.min_row, max_row=area.max_row,
                                           min_col=area.min_col, max_col=area.max_col):
                    for cell in row:
                        cell.value = value
                changed = True
        if not changed:
            return source
        for sheet in list(workbook.chartsheets):
            workbook.remove(sheet)
        for sheet in workbook.worksheets:
            sheet._charts.clear()
        output = BytesIO()
        workbook.save(output)
        return output.getvalue()
    finally:
        workbook.close()


def prepare_excel_rows(source, sheet, frame, first_row_as_header=False):
    frame = frame.astype(object).copy()
    # Fill only real merged ranges. Ordinary blank cells retain their meaning.
    workbook = load_workbook(BytesIO(source), data_only=True)
    try:
        for area in workbook[sheet].merged_cells.ranges:
            value = workbook[sheet].cell(area.min_row, area.min_col).value
            if value is not None:
                for row in range(area.min_row - 1, min(area.max_row, len(frame))):
                    for col in range(area.min_col - 1, min(area.max_col, len(frame.columns))):
                        frame.iat[row, col] = value
    finally:
        workbook.close()
    for col in frame.columns:
        frame[col] = frame[col].map(lambda value: None if isinstance(value, str) and IMAGE_FUNCTION.match(value) else value)
    labels, used = [], set()
    for col in range(len(frame.columns)):
        value = frame.iloc[0, col] if first_row_as_header and len(frame) >= 2 else None
        base = str(value).strip() if pd.notna(value) and str(value).strip() else get_column_letter(col + 1)
        label, suffix = base, 2
        while label in used:
            label = f'{base}_{suffix}'
            suffix += 1
        used.add(label)
        labels.append(label)
    header_rows = int(first_row_as_header and len(frame) >= 2)
    return frame.iloc[header_rows:], labels, header_rows
