"""Synthetic BAC statement text, laid out like pypdf's extraction of the real PDFs (made-up people and numbers),
and a minimal PDF writer so tests can exercise the PDF path without shipping real statements."""

ACCOUNT_TEXT = """Fecha de corte:
30/SEP/26
Banco BAC San José SA
SERVICIO AL CLIENTE
NAME
RESUMEN DE PRODUCTOS
ACTIVOS
TIPO DE PRODUCTO PRODUCTO SALDO MONEDA
CUENTA BANCARIA INVERSIÓN A LA VISTA CR11 0102 0000 1111 2222 33 2,500,000.00 CRC
CUENTA BANCARIA INVERSIÓN A LA VISTA CR44 0102 0000 4444 5555 66 1,000,000.00 CRC
Total 3,500,000.00 CRC
CUENTA BANCARIA INVERSIÓN A LA VISTA CR77 0102 0000 7777 8888 99 150.25 USD
Total 150.25 USD
PASIVOS
TIPO DE PRODUCTO PRODUCTO SALDO MONEDA
CRÉDITO CRÉDITO CR00 0102 0110 0000 0000 00 50,000,000.00 CRC
Total 50,000,000.00 CRC

Nombre: NAME
Cuenta IBAN: CR11 0102 0000 1111 2222 33
Moneda: COLONES
CUADRO RESUMEN
DÉBITOS CRÉDITOS SALDOS
TOTAL MONTO TOTAL MONTO SALDO PROMEDIO SALDO ANTERIOR SALDO A LA FECHA
Cuenta no paga intereses
12 1234,567.89 2 1500,000.00 2,100,000.00 2,234,567.89 2,500,000.00
NO. REFERENCIA FECHA CONCEPTO DÉBITOS CRÉDITOS
000000001 SEP/03 SINPE MOVIL Pulperia 2,000.00
ÚLTIMA LÍNEA SALDO AL CORTE 2,500,000.00

Nombre: NAME
Cuenta IBAN: CR44 0102 0000 4444 5555 66
Moneda: COLONES
CUADRO RESUMEN
DÉBITOS CRÉDITOS SALDOS
TOTAL MONTO TOTAL MONTO SALDO PROMEDIO SALDO ANTERIOR SALDO A LA FECHA
Cuenta no paga intereses
0 .00 1 100,000.00 950,000.00 900,000.00 1,000,000.00

Nombre: NAME
Cuenta IBAN: CR77 0102 0000 7777 8888 99
Moneda: U.S. DOLLAR
CUADRO RESUMEN
DÉBITOS CRÉDITOS SALDOS
TOTAL MONTO TOTAL MONTO SALDO PROMEDIO SALDO ANTERIOR SALDO A LA FECHA
50 5,000 .05
5,001 màs .15
Intereses acreditables mensualmente
3 400.00 1 .25 300.00 550.00 150.25

Datos del crédito
Cuenta IBAN del crédito CR00 0102 0110 0000 0000 00 Tasa interés total anualizada 0.00%
"""

CARD_TEXT = """NAME
NÚMERO DE TARJETA MARCA DE TARJETA PLAN DE LEALTAD CANTIDAD DE
***********4321 AMERICAN EXPRESS MEMBERSHIP REWARDS CRC 0 0.00 10,000.00 300,000.00
TARJETA DE CREDITO
Banco:
BAC San José, S.A. Cédula Jurídica 3-101012009
Marca de tarjeta: MASTER CARD
Número de cuenta: ************5555
Fecha de corte: 24-SET-26
Límite de crédito: USD 1,000.00
Mes y año del estado de cuenta: SET-2026
Detalle pago mínimo Pago de contado
Pago mínimo amortización 1,000.00 2.00 Saldo anterior 90,000.00 20.00
Pagos recibidos 90,000.00- 20.00-
Saldo al corte 50,000.00 0.00
Movimientos de la tarjeta de crédito
Saldo Anterior 25-AGO-26 90,000.00 1,200.00 20.00 0.50
************5555
0907136009498 7-SET-26 PAGO RECIBIDO...136 90,000.00- 1,200.00-
B) Detalle de compras del periodo
************5555 NAME
082599100801 22-AGO-26 DLC*UBER EATS_ SAN JOSE_ CRI CRC 50,000.00
Total de compras del periodo (del 25-AGO-26 al 24-SET-26) 50,000.00 0.00
377711******1111
TARJETA DE CREDITO
Banco:
Marca de tarjeta: AMERICAN EXPRESS
Número de cuenta: ************1111
Fecha de corte: 24-SET-26
Mes y año del estado de cuenta: SET-2026
Pago mínimo amortización 7,000.00 3.00 Saldo anterior 200,000.00 10.00
Pagos recibidos 200,000.00- 0.00
Saldo al corte 300,000.00 45.50
B) Detalle de compras del periodo
************4321 NAME
082599300901 23-AGO-26 PRICE SMART SAN JOSE CRC 200,000.00
082899300901 27-AGO-26 APPLE.COM/BILL CUPERTINO USD 40.00
090199300901 31-AGO-26 APPLE.COM/BILL CUPERTINO USD 4.50-
Total de compras del periodo (del 25-AGO-26 al 24-SET-26) 1,300,000.00 35.50
"""


def make_pdf(text: str) -> bytes:
    """A one-page PDF with `text`, one line per text row (Helvetica, WinAnsi), enough for pypdf to extract."""
    rows = text.splitlines()
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    ops = ["BT", "/F1 7 Tf", "9 TL", "20 1180 Td"] + [f"({esc(r)}) Tj T*" for r in rows] + ["ET"]
    stream = "\n".join(ops).encode("cp1252")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 1200] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)
