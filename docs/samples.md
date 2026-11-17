# Sample invoices

The ten files in [`samples/`](../samples/) and the two in [`pdf_samples/`](../pdf_samples/) are synthetic. The vendors, customers, towns, addresses and numbers are made up, and the business names are deliberately whimsical; phone numbers use the fictional 555-01xx range.

## The ten text and HTML samples

Six mistakes are planted in them on purpose.

| File | Layout | What is in it | What the checker says | Counted as overbilling |
|---|---|---|---|---:|
| `bramblewood_BP-10421.txt` | text, aligned columns | Clean. Includes a wrapped description line. | PASS | |
| `bramblewood_BP-10437.txt` | text, aligned columns | Line items add up to 1,012.40 but the printed subtotal is 1,021.40 (transposed digits). | items don't add up | $9.00 |
| `kestrel_KR-2291.txt` | text, pipe table | Clean. Fractional quantity (2.5 hours). | PASS | |
| `kestrel_KR-2304.txt` | text, pipe table | First line: 12 x 35.00 is printed as 435.00 instead of 420.00. | line total is wrong | $15.00 |
| `kestrel_KR-2310.txt` | text, pipe table | Invoice date typed as 2062 instead of 2026, so it is in the future and after its own due date. | date in the future, due before it was issued | $0.00 |
| `lanternfish_LF-0087.html` | HTML, items table | Clean. | PASS | |
| `lanternfish_LF-0091.html` | HTML, items table | Tax label says 8.875% but the amount is 8% of the subtotal, $5.96 too little. | tax doesn't match its rate | $0.00 |
| `mossgate_MG-5498.html` | HTML, list items | Subtotal + tax = 982.80 but the total says 992.80. | total is wrong | $10.00 |
| `mossgate_MG-5512.html` | HTML, list items | Clean on its own, but see the next file. | sent twice | $0.00 |
| `mossgate_MG-5512_reminder.html` | HTML, list items | The same $1,197.00 invoice sent again as a payment reminder: same vendor, same number, with a `PAYMENT REMINDER - SECOND NOTICE` heading above the vendor's name. | sent twice | $0.00 |

The overbilling total is $9.00 + $15.00 + $10.00 = $34.00.

MG-5512 is a real $1,197.00 invoice that arrived twice. Paying both copies would pay it twice, so both are held and a person picks the one to pay. That is why six mistakes hold seven invoices. It is reported on its own line ("Sent twice: a $1,197.00 invoice, held so it is not paid twice"), not as overbilling: nothing on the invoice is wrong.

`tests/test_end_to_end.py` holds the expected verdict, reason codes, overbilling amount and sent-twice amount for each file.

## The two PDF samples

`tools/make_pdf_samples.py` writes these with the standard library only. Each is a one-page PDF with a text layer, laid out the way invoicing software does it: every label, description and amount is placed on the page by itself, with no spaces between the columns. Reading them needs pypdf (`pip install pypdf`).

| File | What is in it | What the checker says | Counted as overbilling |
|---|---|---|---:|
| `tumblewick_TW-3301.pdf` | Clean: four lines, 6% tax, total $477.00. | PASS | |
| `tumblewick_TW-3318.pdf` | Second line: 2 x 36.75 is printed as 75.30 instead of 73.50 (two digits swapped). The wrong line is carried into the subtotal, tax and total, so only the line itself shows it. | line total is wrong | $1.80 |

`tests/test_pdf.py` holds the expected results. It also checks that the two files are exactly what the script writes.
