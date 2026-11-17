# Invoice review report

- Checked: 10 invoices from `samples` (extractor: `rules`, dates checked against 2026-09-21)
- Result: 3 PASS, 7 NEEDS REVIEW, 0 NOT CHECKED
- Held for review: 7 invoices totalling $6,643.95
- Overbilling found: $34.00 in arithmetic errors
- Sent twice: a $1,197.00 invoice, held so it is not paid twice
- Only PASS invoices are in `import_ready.csv`.

## Needs a human (7)

### bramblewood_BP-10437.txt

Vendor: Bramblewood Paper Co. - Invoice: BP-10437 - Date: 2026-08-17 - Total: $1,095.45 - Overbilled by: $9.00

- **Items don't add up.** Line items add up to $1,012.40 but the subtotal says $1,021.40 (off by $9.00). `lines_sum`

### kestrel_KR-2304.txt

Vendor: Kestrelrook IT Services - Invoice: KR-2304 - Date: 2026-08-25 - Total: $633.34 - Overbilled by: $15.00

- **Line total is wrong.** Item 1 ('Managed support, per seat'): 12 x $35.00 = $420.00, but the line shows $435.00 (off by $15.00). `line_math`

### kestrel_KR-2310.txt

Vendor: Kestrelrook IT Services - Invoice: KR-2310 - Date: 2062-09-18 - Total: $792.88

- **Date in the future.** Invoice date 2062-09-18 is in the future (checked against 2026-09-21); check the year for a typo. `date_future`
- **Due before it was issued.** Due date 2026-10-18 is before the invoice date 2062-09-18. `due_before_issue`

### lanternfish_LF-0091.html

Vendor: Lanternfish Catering LLC - Invoice: LF-0091 - Date: 2026-09-02 - Total: $735.48

- **Tax doesn't match its rate.** Tax is $54.48, but 8.875% of the $681.00 subtotal is $60.44 (off by $5.96). `tax_rate`

### mossgate_MG-5498.html

Vendor: Mossgate Janitorial Services - Invoice: MG-5498 - Date: 2026-07-31 - Total: $992.80 - Overbilled by: $10.00

- **Total is wrong.** Subtotal $936.00 + tax $46.80 = $982.80, but the total says $992.80 (off by $10.00). `total_math`

### mossgate_MG-5512.html

Vendor: Mossgate Janitorial Services - Invoice: MG-5512 - Date: 2026-08-14 - Total: $1,197.00

- **Sent twice.** Invoice number MG-5512 from Mossgate Janitorial Services also appears in mossgate_MG-5512_reminder.html; paying both would pay it twice. `duplicate_number`

### mossgate_MG-5512_reminder.html

Vendor: Mossgate Janitorial Services - Invoice: MG-5512 - Date: 2026-08-14 - Total: $1,197.00 - Second copy of a $1,197.00 invoice: paying this copy too would pay it twice

- **Sent twice.** Invoice number MG-5512 from Mossgate Janitorial Services also appears in mossgate_MG-5512.html; paying both would pay it twice. `duplicate_number`

## Not checked (0)

Every file in the folder was read.

## Passed (3)

| File | Vendor | Invoice | Date | Lines | Total |
|---|---|---|---|---:|---:|
| bramblewood_BP-10421.txt | Bramblewood Paper Co. | BP-10421 | 2026-08-03 | 4 | $435.76 |
| kestrel_KR-2291.txt | Kestrelrook IT Services | KR-2291 | 2026-08-11 | 4 | $889.85 |
| lanternfish_LF-0087.html | Lanternfish Catering LLC | LF-0087 | 2026-08-20 | 4 | $1,350.05 |

## What every invoice was checked for

- `extraction` (not fully read): the extractor recognised the layout and read every item row
- `required_fields` (details missing): vendor, invoice number, invoice date, at least one line item and a total are present
- `line_incomplete` (line incomplete): every line item has an amount, and a quantity and unit price either both or neither
- `line_math` (line total is wrong): quantity x unit price = line amount, within a cent (when both are printed)
- `lines_sum` (items don't add up): the line amounts add up to the subtotal, within a cent (to total minus tax when no subtotal is printed)
- `tax_rate` (tax doesn't match its rate): when a tax rate is printed, tax = rate x subtotal (a cent of rounding allowed per two lines)
- `total_math` (total is wrong): subtotal + tax = total, within a cent
- `total_not_positive` (zero or negative total): the total is above zero (credit notes and refunds are left to a person)
- `date_future` (date in the future): the invoice date is not after the run date
- `date_stale` (very old invoice): the invoice date is not older than --max-age-days (365 by default; 0 turns this off)
- `date_unreadable` (due date unreadable): a printed due date can be read as a date
- `due_before_issue` (due before it was issued): the due date is not before the invoice date
- `duplicate_number` (sent twice): no other file in the batch has the same vendor and invoice number
- `possible_duplicate` (may be a duplicate): no other file has the same vendor, date and total under a different invoice number, or the same invoice number with the same total or date under a different vendor name
- `already_seen` (seen in an earlier run): the vendor and invoice number are not in the --history file of earlier runs
- `not_in_source` (number not on the page): (AI extraction only) every amount and the invoice number appear in the source document
