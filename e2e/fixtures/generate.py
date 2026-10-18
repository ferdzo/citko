#!/usr/bin/env python3
"""Create tiny, original, text-bearing EPUB/PDF fixtures using Python stdlib.

Both fixtures carry real navigation (EPUB NCX table of contents, PDF outline) so reader features
that depend on chapters — contents panel, chapter progress — have something to resolve.
"""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED, ZIP_STORED

OUT = Path(__file__).parent
UID = 'urn:uuid:00000000-0000-4000-8000-000000000001'
chapters = [
    ("The River", "The river keeps the morning light. This is a public-domain test story written for Citko browser integration evidence. The water carries a silver ribbon beneath the old bridge."),
    ("The Orchard", "In the orchard, every apple remembers the rain. Mara opens her notebook and records the quiet names of trees, one by one, before the autumn wind arrives."),
    ("A Lantern", "At dusk a lantern shines beside the river. The travelers find their way home, carrying stories that will be read again on another device."),
]

def epub():
    path = OUT / 'citko-evidence.epub'
    with ZipFile(path, 'w') as z:
        z.writestr('mimetype', 'application/epub+zip', compress_type=ZIP_STORED)
        z.writestr('META-INF/container.xml', '''<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>''')
        items = ''.join(f'<item id="c{i}" href="chapter{i}.xhtml" media-type="application/xhtml+xml"/>' for i in range(1, 4))
        spine = ''.join(f'<itemref idref="c{i}"/>' for i in range(1, 4))
        z.writestr('OEBPS/content.opf', f'''<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">{UID}</dc:identifier><dc:title>Citko Evidence: A Small Journey</dc:title><dc:creator>Citko Test Authors</dc:creator><dc:language>en</dc:language></metadata><manifest>{items}<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/></manifest><spine toc="ncx">{spine}</spine></package>''')
        navpoints = ''.join(
            f'<navPoint id="n{i}" playOrder="{i}"><navLabel><text>{title}</text></navLabel><content src="chapter{i}.xhtml"/></navPoint>'
            for i, (title, _) in enumerate(chapters, 1))
        z.writestr('OEBPS/toc.ncx', f'''<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head><meta name="dtb:uid" content="{UID}"/></head><docTitle><text>Citko Evidence: A Small Journey</text></docTitle><navMap>{navpoints}</navMap></ncx>''')
        for i, (title, body) in enumerate(chapters, 1):
            z.writestr(f'OEBPS/chapter{i}.xhtml', f'<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>{title}</title></head><body><h1>{title}</h1><p>{body}</p></body></html>')

def pdf():
    # Basic PDF 1.4 using Helvetica and text operators; text remains selectable.
    objects = [None, None]  # 1 = catalog, 2 = page tree, both filled once the page count is known.
    for idx, (title, body) in enumerate(chapters):
        page_obj = 3 + idx * 2
        stream_obj = page_obj + 1
        content = f'BT /F1 20 Tf 72 740 Td ({title}) Tj /F1 12 Tf 0 -32 Td ({body}) Tj ET'
        objects.extend([f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >> /Contents {stream_obj} 0 R >>', f'<< /Length {len(content)} >>\nstream\n{content}\nendstream'])
    first_outline = 3 + len(chapters) * 2  # first object number after the pages
    objects[0] = f'<< /Type /Catalog /Pages 2 0 R /Outlines {first_outline} 0 R >>'
    objects[1] = f'<< /Type /Pages /Kids [{" ".join(f"{3 + i * 2} 0 R" for i in range(len(chapters)))}] /Count {len(chapters)} >>'
    # A flat outline of one entry per page, so the reader can resolve chapter -> page -> fraction.
    entries = []
    for idx, (title, _) in enumerate(chapters):
        number = first_outline + 1 + idx
        parts = [f'/Title ({title})', f'/Parent {first_outline} 0 R', f'/Dest [{3 + idx * 2} 0 R /Fit]']
        if idx > 0:
            parts.append(f'/Prev {number - 1} 0 R')
        if idx < len(chapters) - 1:
            parts.append(f'/Next {number + 1} 0 R')
        entries.append('<< ' + ' '.join(parts) + ' >>')
    objects.append(f'<< /Type /Outlines /First {first_outline + 1} 0 R /Last {first_outline + len(chapters)} 0 R /Count {len(chapters)} >>')
    objects.extend(entries)
    data = bytearray(b'%PDF-1.4\n')
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(data)); data.extend(f'{i} 0 obj\n{obj}\nendobj\n'.encode())
    xref = len(data); data.extend(f'xref\n0 {len(objects) + 1}\n0000000000 65535 f \n'.encode())
    for offset in offsets[1:]: data.extend(f'{offset:010d} 00000 n \n'.encode())
    data.extend(f'trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
    (OUT / 'citko-evidence.pdf').write_bytes(data)

if __name__ == '__main__':
    epub(); pdf()
