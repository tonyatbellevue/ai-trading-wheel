"""Word lists used by the heuristic (non-checksum) detectors.

These exist so that name and address detection can run with zero network access
and no multi-hundred-megabyte model. They are deliberately small and biased
toward the false-negative side: a missed low-confidence name still shows up in
the review list via the generic capitalised-sequence rule, whereas a noisy
gazetteer would bury the user in false positives.
"""

from __future__ import annotations

# Words that start with a capital in normal prose and must never on their own
# be treated as a person's name.
STOP_TITLECASE = {
    # document furniture
    "Abstract", "Account", "Address", "Agreement", "Amount", "Annex", "Appendix",
    "Applicant", "Application", "Attachment", "Authority", "Balance", "Bank",
    "Branch", "Bureau", "Card", "Cash", "Certificate", "Chapter", "Charge",
    "City", "Claim", "Client", "Code", "Company", "Confidential", "Contact",
    "Contract", "Copy", "Corporation", "Country", "Credit", "Currency",
    "Customer", "Date", "Debit", "Department", "Deposit", "Description",
    "Details", "Disclaimer", "Document", "Draft", "Due", "Duplicate", "Email",
    "Employee", "Employer", "Exhibit", "Expiry", "Figure", "File", "Form",
    "Gross", "Group", "Holder", "Identity", "Income", "Insurance", "Interest",
    "Invoice", "Issue", "Issued", "Item", "Legal", "Letter", "Limited",
    "Loan", "Mobile", "Name", "National", "Net", "Note", "Notice", "Number",
    "Office", "Order", "Original", "Page", "Passport", "Payment", "Period",
    "Personal", "Phone", "Plan", "Policy", "Postal", "Prepared", "Price",
    "Private", "Product", "Profile", "Quantity", "Rate", "Receipt",
    "Reference", "Registration", "Report", "Request", "Required", "Revenue",
    "Review", "Sample", "Schedule", "Section", "Security", "Service",
    "Signature", "Statement", "Status", "Street", "Subject", "Subtotal",
    "Summary", "Table", "Tax", "Telephone", "Term", "Terms", "Title", "Total",
    "Transaction", "Transfer", "Type", "Unit", "Valid", "Value", "Version",
    "Amount", "Balance", "Payable", "Receivable", "Withdrawal", "Branch",
    "Swift", "Routing", "Sort", "Currency", "Cheque", "Check", "Remarks",
    "Notes", "Attention", "Regards", "Sincerely", "Dear", "From", "Subject",
    "Page", "Continued", "Confidentiality", "Warning", "Important", "Please",
    "Thank", "Thanks", "Yours", "Faithfully", "Signed", "Witness", "Date",
    "Place", "Purpose", "Category", "Class", "Grade", "Level", "Score",
    "Result", "Test", "Sample", "Batch", "Lot", "Serial", "Model", "Brand",
    "Record", "Records", "Patient", "Physician", "Doctor", "Nurse", "Meeting",
    "Board", "Quarterly", "Annual", "Monthly", "Weekly", "Daily", "Revenue",
    "Margin", "Profit", "Loss", "Expense", "Budget", "Forecast", "Actual",
    "Opening", "Closing", "Beginning", "Ending", "Previous", "Current",
    "Next", "Last", "First", "Second", "Final", "Initial", "Draft",
    # past participles that introduce a signatory - they precede a name but
    # are not part of it ("Assessed by Dr Lim", "Approved By Manager")
    "Assessed", "Approved", "Authorised", "Authorized", "Certified",
    "Checked", "Completed", "Confirmed", "Endorsed", "Issued", "Received",
    "Reviewed", "Signed", "Submitted", "Validated", "Verified", "Witnessed",
    "Attended", "Attending", "Processed", "Updated", "Created", "Modified",
    # calendar
    "January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December", "Monday", "Tuesday",
    "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "Jan", "Feb",
    "Mar", "Apr", "Jun", "Jul", "Aug", "Sep", "Sept", "Oct", "Nov", "Dec",
    # frequent function words that can be title-cased in headings
    "The", "And", "For", "With", "This", "That", "All", "Any", "New", "Not",
    "Per", "Via", "Our", "Your", "Their", "His", "Her", "You", "We", "It",
    "Is", "Are", "Was", "Were", "Be", "Been", "Has", "Have", "Had", "Will",
    "Shall", "May", "Can", "Must", "Should", "Would", "Could", "If", "Then",
    "When", "Where", "Which", "Who", "What", "How", "Why", "Such", "Same",
    "Other", "Each", "Both", "More", "Most", "Less", "Least", "One", "Two",
    "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten",
    # org suffixes - a company name is not a personal name
    "Inc", "Ltd", "Llc", "Llp", "Plc", "Pte", "Sdn", "Bhd", "Gmbh", "Corp",
    "Holdings", "Partners", "Associates", "Solutions", "Systems", "Services",
    "Technologies", "International", "Global", "Regional", "Trust", "Fund",
    "Capital", "Ventures", "Industries", "Enterprises", "Institute",
    "University", "College", "School", "Hospital", "Clinic", "Centre",
    "Center", "Foundation", "Association", "Society", "Council", "Committee",
    "Ministry", "Government", "Republic", "Kingdom", "State", "Province",
    # common countries / places that appear in addresses but are not names
    "Singapore", "Malaysia", "Indonesia", "China", "Japan", "Korea", "India",
    "Australia", "Canada", "America", "United", "States", "Britain",
    "England", "Scotland", "Ireland", "France", "Germany", "Italy", "Spain",
    "Netherlands", "Switzerland", "Sweden", "Norway", "Denmark", "Finland",
    "Hong", "Kong", "Taiwan", "Thailand", "Vietnam", "Philippines",
    "New York", "London", "Tokyo", "Beijing", "Shanghai", "Shenzhen",
}

HONORIFICS = (
    "Mr", "Mrs", "Ms", "Miss", "Mx", "Dr", "Prof", "Professor", "Sir",
    "Madam", "Mdm", "Rev", "Hon", "Capt", "Col", "Maj", "Lt", "Sgt",
)

# Labels that introduce a person's name on a form.
NAME_LABELS = (
    "name", "full name", "fullname", "legal name", "given name", "first name",
    "last name", "surname", "family name", "middle name", "preferred name",
    "account name", "account holder", "accountholder", "holder", "cardholder",
    "card holder", "applicant", "applicant name", "customer", "customer name",
    "client", "client name", "patient", "patient name", "employee",
    "employee name", "beneficiary", "payee", "payer", "borrower", "tenant",
    "landlord", "guarantor", "signatory", "signed by", "prepared by",
    "authorised by", "authorized by", "attention", "attn", "contact person",
    "next of kin", "emergency contact", "father", "mother", "spouse",
    "姓名", "名字", "全名", "户名", "账户名", "客户姓名", "持卡人", "申请人",
    "联系人", "收款人", "付款人", "法定代表人", "姓 名",
)

ADDRESS_LABELS = (
    "address", "addr", "residential address", "home address", "mailing address",
    "billing address", "shipping address", "delivery address",
    "correspondence address", "registered address", "street address",
    "permanent address", "住址", "地址", "住 址", "通讯地址", "邮寄地址",
    "户籍地址", "联系地址",
)

PHONE_LABELS = (
    "phone", "telephone", "tel", "mobile", "mob", "cell", "cellphone", "hp",
    "handphone", "contact number", "contact no", "fax", "whatsapp",
    "电话", "手机", "联系电话", "手机号", "传真", "座机",
)

DOB_LABELS = (
    "dob", "d.o.b", "date of birth", "birth date", "birthdate", "born on",
    "born", "birthday", "出生日期", "出生年月", "生日", "出生",
)

ACCOUNT_LABELS = (
    "account", "account no", "account number", "acct", "acct no", "a/c",
    "a/c no", "bank account", "iban", "sort code", "routing number",
    "routing no", "aba", "swift", "bic", "policy number", "policy no",
    "member number", "member no", "membership no", "customer id",
    "customer number", "reference number", "beneficiary account",
    "账号", "银行账号", "账户", "卡号", "开户行", "保单号", "会员号",
)

ID_LABELS = (
    "nric", "fin", "nric/fin", "nric no", "identity card", "ic", "ic no",
    "id no", "id number", "identification", "identity number", "national id",
    "ssn", "social security", "social security number", "aadhaar", "mykad",
    "hkid", "身份证", "身份证号", "证件号", "证件号码", "身份证号码",
)

PASSPORT_LABELS = (
    "passport", "passport no", "passport number", "passport #", "travel document",
    "护照", "护照号", "护照号码",
)

TAX_LABELS = (
    "tax id", "tin", "tax identification", "ein", "vat", "vat no", "gst",
    "gst no", "uen", "abn", "utr", "纳税人识别号", "税号",
)

# Street-type tokens for address shape matching.
STREET_TYPES = (
    "street", "st", "avenue", "ave", "road", "rd", "boulevard", "blvd", "lane",
    "ln", "drive", "dr", "court", "ct", "way", "terrace", "ter", "place", "pl",
    "crescent", "cres", "close", "walk", "link", "rise", "grove", "park",
    "square", "sq", "highway", "hwy", "parkway", "pkwy", "circle", "cir",
    "trail", "loop", "alley", "quay", "wharf", "gardens", "green", "heights",
    "view", "vista", "ridge", "hill", "bank", "row", "mews", "circus",
)

UNIT_WORDS = (
    "apt", "apartment", "unit", "suite", "ste", "floor", "flr", "level", "lvl",
    "room", "rm", "block", "blk", "tower", "building", "bldg", "house", "flat",
)

# A very small first-name gazetteer. Hitting it lifts a candidate out of the
# low-confidence bucket; missing it does not disqualify anything.
COMMON_FIRST_NAMES = {
    "james", "john", "robert", "michael", "william", "david", "richard",
    "joseph", "thomas", "charles", "christopher", "daniel", "matthew",
    "anthony", "mark", "donald", "steven", "paul", "andrew", "joshua",
    "kenneth", "kevin", "brian", "george", "timothy", "ronald", "jason",
    "edward", "jeffrey", "ryan", "jacob", "gary", "nicholas", "eric",
    "jonathan", "stephen", "larry", "justin", "scott", "brandon", "benjamin",
    "samuel", "gregory", "alexander", "patrick", "frank", "raymond", "jack",
    "dennis", "jerry", "tyler", "aaron", "jose", "adam", "henry", "nathan",
    "douglas", "zachary", "peter", "kyle", "walter", "ethan", "jeremy",
    "harold", "keith", "christian", "roger", "noah", "gerald", "carl",
    "terry", "sean", "austin", "arthur", "lawrence", "jesse", "dylan",
    "bryan", "joe", "jordan", "billy", "bruce", "albert", "willie", "gabriel",
    "logan", "alan", "juan", "wayne", "roy", "ralph", "randy", "eugene",
    "vincent", "russell", "elijah", "louis", "bobby", "philip", "johnny",
    "mary", "patricia", "jennifer", "linda", "elizabeth", "barbara",
    "susan", "jessica", "sarah", "karen", "nancy", "lisa", "betty",
    "margaret", "sandra", "ashley", "kimberly", "emily", "donna", "michelle",
    "carol", "amanda", "dorothy", "melissa", "deborah", "stephanie",
    "rebecca", "sharon", "laura", "cynthia", "kathleen", "amy", "angela",
    "shirley", "anna", "brenda", "pamela", "emma", "nicole", "helen",
    "samantha", "katherine", "christine", "debra", "rachel", "carolyn",
    "janet", "catherine", "maria", "heather", "diane", "ruth", "julie",
    "olivia", "joyce", "virginia", "victoria", "kelly", "lauren", "christina",
    "joan", "evelyn", "judith", "megan", "andrea", "cheryl", "hannah",
    "jacqueline", "martha", "gloria", "teresa", "ann", "sara", "madison",
    "frances", "kathryn", "janice", "jean", "abigail", "alice", "julia",
    "judy", "sophia", "grace", "denise", "amber", "doris", "marilyn",
    "danielle", "beverly", "isabella", "theresa", "diana", "natalie",
    "brittany", "charlotte", "marie", "kayla", "alexis", "lori",
    # common Chinese / Malay / Indian given names seen on SG documents
    "wei", "ming", "hui", "jun", "yan", "xin", "lei", "jie", "feng", "yu",
    "chen", "li", "wang", "zhang", "liu", "tan", "lim", "lee", "ng", "ong",
    "goh", "teo", "chua", "sim", "koh", "wong", "chan", "cheong", "yeo",
    "muhammad", "mohamed", "mohammad", "ahmad", "abdul", "siti", "nur",
    "nurul", "aisyah", "fatimah", "hassan", "ibrahim", "ismail", "yusof",
    "rajesh", "suresh", "ramesh", "anil", "sunil", "vijay", "arun", "kumar",
    "priya", "deepa", "meena", "lakshmi", "ravi", "sanjay", "amit", "rahul",
}

NAME_PARTICLES = {
    "bin", "binte", "binti", "bt", "b", "van", "von", "der", "den", "de",
    "del", "della", "di", "da", "dos", "du", "la", "le", "el", "al", "ibn",
    "mac", "mc", "o", "st", "san", "santa", "jr", "sr", "ii", "iii", "iv",
}
