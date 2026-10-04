"""BAC Credomatic (Costa Rica) statements, as emailed by estadodecuenta@baccredomatic.cr.

* "Estado de cuenta de cuenta(s) bancaria(s)": one section per account ("Cuenta IBAN: CR.."), each with a
  CUADRO RESUMEN line: debit count, debit total, credit count, credit total, average, previous and
  current balance. A "RESUMEN DE PRODUCTOS" block on page 1 totals assets and liabilities per currency.
* "Estado de cuenta Tarjeta de Crédito": one section per card account ("Marca de tarjeta: .."), with the
  period's purchases ("Total de compras del periodo (del 25-AGO-26 al 24-SET-26)"), payments received,
  previous balance and balance at the cut-off, each in colones and dollars.
"""

import re

from fraudalert.statements import (_AMOUNT, Statement, StatementError, StatementLine, StatementParser,
                                   amount, es_date, mask)

CURRENCIES = {"COLONES": "CRC", "U.S. DOLLAR": "USD", "DOLARES": "USD", "DÓLARES": "USD", "EUROS": "EUR"}
_SUMMARY = re.compile(rf"(?m)^(\d+) ({_AMOUNT}) (\d+) ({_AMOUNT}) ({_AMOUNT}) ({_AMOUNT}) ({_AMOUNT})\s*$")


class BacAccountStatement(StatementParser):
    bank, kind = "BAC Credomatic", "account"

    def detect(self, text: str) -> bool:
        return "CUADRO RESUMEN" in text and "SALDO A LA FECHA" in text and "Cuenta IBAN:" in text

    def parse(self, text: str) -> Statement:
        m = re.search(r"Fecha de corte:\s*(\d{1,2})/([A-Za-z]{3})/(\d{2,4})", text)
        if not m:
            raise StatementError("no 'Fecha de corte' in the account statement")
        cut = es_date(*m.groups())
        lines: list[StatementLine] = []
        starts = [s.start() for s in re.finditer(r"Cuenta IBAN:", text)]
        for i, start in enumerate(starts):
            section = text[start: starts[i + 1] if i + 1 < len(starts) else len(text)]
            iban = re.match(r"Cuenta IBAN:\s*([A-Z]{2}[\d ]+)", section)
            cur = re.search(r"Moneda:\s*(.+)", section)
            summary = _SUMMARY.search(section[section.find("CUADRO RESUMEN"):]) if "CUADRO RESUMEN" in section else None
            if not (iban and cur and summary):
                continue
            _, debits, _, credits, _, opening, closing = summary.groups()
            line = StatementLine(
                kind="account", label=f"Account ****{mask(iban.group(1))}", last4=mask(iban.group(1)),
                currency=CURRENCIES.get(cur.group(1).strip().upper(), cur.group(1).strip()[:3].upper()),
                opening=amount(opening), closing=amount(closing), debits=amount(debits), credits=amount(credits))
            line.verified = abs(line.opening - line.debits + line.credits - line.closing) < 0.02
            lines.append(line)
        lines += _product_totals(text)
        if not any(line.kind == "account" for line in lines):
            raise StatementError("found no account summaries in the account statement")
        return Statement(self.bank, self.kind, cut.replace(day=1), None, cut, lines)


def _product_totals(text: str) -> list[StatementLine]:
    """'RESUMEN DE PRODUCTOS': the 'Total <amount> <currency>' lines under ACTIVOS and PASIVOS."""
    start = text.find("RESUMEN DE PRODUCTOS")
    if start < 0:
        return []
    block = text[start: start + 4000]
    liab = block.find("PASIVOS")
    out = []
    for kind, part in (("assets", block[:liab if liab > 0 else len(block)]), ("liabilities", block[liab:] if liab > 0 else "")):
        part = part.split("\n\n")[0] if kind == "liabilities" else part
        for value, currency in re.findall(rf"(?m)^Total ({_AMOUNT}) ([A-Z]{{3}})\s*$", part):
            out.append(StatementLine(kind=kind, label="Assets" if kind == "assets" else "Liabilities",
                                     currency=currency, closing=amount(value)))
    return out


class BacCardStatement(StatementParser):
    bank, kind = "BAC Credomatic", "card"

    def detect(self, text: str) -> bool:
        return "TARJETA DE CREDITO" in text and "Total de compras del periodo" in text

    def parse(self, text: str) -> Statement:
        starts = [s.start() for s in re.finditer(r"Marca de tarjeta:", text)]
        lines: list[StatementLine] = []
        period_start = period_end = month = None
        for i, start in enumerate(starts):
            section = text[start: starts[i + 1] if i + 1 < len(starts) else len(text)]
            brand = re.match(r"Marca de tarjeta:\s*(.+)", section).group(1).strip()
            account = re.search(r"Número de cuenta:\s*[*\d]*?(\d{4})\s", section)
            cut = re.search(r"Fecha de corte:\s*(\d{1,2})-([A-Za-z]{3})-(\d{2,4})", section)
            purchases = re.search(rf"Total de compras del periodo \(del (\d{{1,2}})-([A-Za-z]{{3}})-(\d{{2,4}}) al "
                                  rf"(\d{{1,2}})-([A-Za-z]{{3}})-(\d{{2,4}})\)\s+({_AMOUNT})\s+({_AMOUNT})", section)
            if not (account and cut and purchases):
                continue
            g = purchases.groups()
            period_start, period_end = es_date(*g[0:3]), es_date(*g[3:6])
            ym = re.search(r"Mes y año del estado de cuenta:\s*([A-Za-z]{3})-(\d{4})", section)
            month = es_date("1", *ym.groups()) if ym else period_end.replace(day=1)

            def pair(label):  # "<label> <colones> <dollars>", e.g. "Saldo al corte 127,754.77 39.80"
                p = re.search(rf"{label} ({_AMOUNT}) ({_AMOUNT})", section)
                return (amount(p.group(1)), amount(p.group(2))) if p else (None, None)

            opening, payments, closing = pair("Saldo anterior"), pair("Pagos recibidos"), pair("Saldo al corte")
            cards = sorted(set(re.findall(r"(?m)^\*{6,}(\d{4})\b", section))) or [account.group(1)]
            for idx, currency in enumerate(("CRC", "USD")):
                values = (opening[idx], closing[idx], amount(g[6 + idx]), payments[idx])
                if not any(values):
                    continue
                lines.append(StatementLine(
                    kind="card", label=f"{brand.title()} ****{account.group(1)}", currency=currency,
                    last4=account.group(1), cards=cards, opening=values[0], closing=values[1],
                    debits=values[2], credits=abs(values[3]) if values[3] is not None else None))
        if not lines:
            raise StatementError("found no card summaries in the credit-card statement")
        return Statement(self.bank, self.kind, month, period_start, period_end, lines)
