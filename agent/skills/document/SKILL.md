---
name: document
description: 生成各种格式的文档文件，包括 Excel 表格、PDF 文档、Word 文档和 PPT 演示文稿。当用户需要生成、导出、保存数据为文件时使用此技能。
---

# 文档生成

生成 Excel/PDF/Word/PPT 文件，保存在 `document_output/` 目录下。

脚本：
- `save_excel` — Excel 表格，参数：headers（表头）, rows（数据行）, filename, formatting（可选）
- `save_pdf` — PDF 文档，参数：content, filename, formatting（可选）
- `save_word` — Word 文档，参数：content, filename, formatting（可选）
- `save_ppt` — PPT 演示，参数：content, filename, formatting（可选）