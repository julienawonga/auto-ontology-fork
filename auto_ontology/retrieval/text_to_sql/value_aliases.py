# SPDX-License-Identifier: Apache-2.0

"""Well-known aliases of values, as plain data.

"USA", "U.S." and "America" are all "United States", but no embedding model is
guaranteed to know it and nothing here can verify one. This table answers the
common cases deterministically: ISO 3166 alpha-2 and alpha-3 codes and the usual
short names of countries, and the postal abbreviations of the US states.

It only *proposes candidates*. The lookup in ``value_linking`` turns a candidate
into an anchor only when that exact value is stored in an indexed column, so an
alias never invents a value the database does not hold.

Each line is one entity: ``fields separated by |``. Every field is an alias of
every other field of the line, and the names, then the codes, are the
spellings the entity may be stored under (datasets differ: "Russian Federation",
"Russia"). An alias belonging to several entities ("CA" is Canada and
California) is ambiguous, and the lookup reports every stored reading.
"""

from __future__ import annotations

from auto_ontology.retrieval.text_to_sql.value_text import normalize_value

# alpha-2 | alpha-3 | stored spellings...
_COUNTRIES = """
AF|AFG|Afghanistan
AX|ALA|Aland Islands
AL|ALB|Albania
DZ|DZA|Algeria
AS|ASM|American Samoa
AD|AND|Andorra
AO|AGO|Angola
AI|AIA|Anguilla
AQ|ATA|Antarctica
AG|ATG|Antigua and Barbuda
AR|ARG|Argentina
AM|ARM|Armenia
AW|ABW|Aruba
AU|AUS|Australia
AT|AUT|Austria
AZ|AZE|Azerbaijan
BS|BHS|Bahamas|The Bahamas
BH|BHR|Bahrain
BD|BGD|Bangladesh
BB|BRB|Barbados
BY|BLR|Belarus
BE|BEL|Belgium
BZ|BLZ|Belize
BJ|BEN|Benin
BL|BLM|Saint Barthelemy
BM|BMU|Bermuda
BT|BTN|Bhutan
BO|BOL|Bolivia|Bolivia, Plurinational State of
BA|BIH|Bosnia and Herzegovina|Bosnia
BW|BWA|Botswana
BV|BVT|Bouvet Island
BQ|BES|Bonaire, Sint Eustatius and Saba|Bonaire
BR|BRA|Brazil|Brasil
IO|IOT|British Indian Ocean Territory
BN|BRN|Brunei|Brunei Darussalam
BG|BGR|Bulgaria
BF|BFA|Burkina Faso
BI|BDI|Burundi
KH|KHM|Cambodia
CM|CMR|Cameroon
CA|CAN|Canada
CV|CPV|Cape Verde|Cabo Verde
KY|CYM|Cayman Islands
CF|CAF|Central African Republic
TD|TCD|Chad
CL|CHL|Chile
CN|CHN|China|People's Republic of China
CX|CXR|Christmas Island
CC|CCK|Cocos (Keeling) Islands
CO|COL|Colombia
KM|COM|Comoros
CG|COG|Congo|Republic of the Congo|Congo-Brazzaville
CD|COD|Congo, The Democratic Republic of the|Democratic Republic of the Congo|DR Congo|DRC
CK|COK|Cook Islands
CR|CRI|Costa Rica
CI|CIV|Cote d'Ivoire|Ivory Coast
HR|HRV|Croatia
CU|CUB|Cuba
CW|CUW|Curacao
CY|CYP|Cyprus
CZ|CZE|Czech Republic|Czechia
DK|DNK|Denmark
DJ|DJI|Djibouti
DM|DMA|Dominica
DO|DOM|Dominican Republic
EC|ECU|Ecuador
EG|EGY|Egypt
SV|SLV|El Salvador
GQ|GNQ|Equatorial Guinea
ER|ERI|Eritrea
EE|EST|Estonia
SZ|SWZ|Eswatini|Swaziland
ET|ETH|Ethiopia
FK|FLK|Falkland Islands (Malvinas)|Falkland Islands
FO|FRO|Faroe Islands
FJ|FJI|Fiji
FI|FIN|Finland
FR|FRA|France
GF|GUF|French Guiana
PF|PYF|French Polynesia
GA|GAB|Gabon
GM|GMB|Gambia|The Gambia
GE|GEO|Georgia
DE|DEU|Germany|Deutschland
GH|GHA|Ghana
GI|GIB|Gibraltar
GR|GRC|Greece
GL|GRL|Greenland
GD|GRD|Grenada
GP|GLP|Guadeloupe
GU|GUM|Guam
GS|SGS|South Georgia and the South Sandwich Islands
GT|GTM|Guatemala
GG|GGY|Guernsey
GN|GIN|Guinea
GW|GNB|Guinea-Bissau
GY|GUY|Guyana
HT|HTI|Haiti
VA|VAT|Holy See (Vatican City State)|Vatican City|Vatican
HM|HMD|Heard Island and McDonald Islands
HN|HND|Honduras
HK|HKG|Hong Kong
HU|HUN|Hungary
IS|ISL|Iceland
IN|IND|India
ID|IDN|Indonesia
IR|IRN|Iran|Iran, Islamic Republic of
IQ|IRQ|Iraq
IE|IRL|Ireland
IM|IMN|Isle of Man
IL|ISR|Israel
IT|ITA|Italy
JM|JAM|Jamaica
JP|JPN|Japan
JE|JEY|Jersey
JO|JOR|Jordan
KZ|KAZ|Kazakhstan
KE|KEN|Kenya
KI|KIR|Kiribati
KP|PRK|North Korea|Korea, Democratic People's Republic of
KR|KOR|South Korea|Korea, Republic of|Korea
KW|KWT|Kuwait
KG|KGZ|Kyrgyzstan
LA|LAO|Laos|Lao People's Democratic Republic
LV|LVA|Latvia
LB|LBN|Lebanon
LS|LSO|Lesotho
LR|LBR|Liberia
LY|LBY|Libya|Libyan Arab Jamahiriya
LI|LIE|Liechtenstein
LT|LTU|Lithuania
LU|LUX|Luxembourg
MO|MAC|Macao|Macau
MK|MKD|North Macedonia|Macedonia|Macedonia, the former Yugoslav Republic of
MF|MAF|Saint Martin (French part)|Saint Martin
MG|MDG|Madagascar
MW|MWI|Malawi
MY|MYS|Malaysia
MV|MDV|Maldives
ML|MLI|Mali
MT|MLT|Malta
MH|MHL|Marshall Islands
MQ|MTQ|Martinique
MR|MRT|Mauritania
MU|MUS|Mauritius
YT|MYT|Mayotte
MX|MEX|Mexico
FM|FSM|Micronesia|Micronesia, Federated States of
MD|MDA|Moldova|Moldova, Republic of
MC|MCO|Monaco
MN|MNG|Mongolia
ME|MNE|Montenegro
MS|MSR|Montserrat
MA|MAR|Morocco
MZ|MOZ|Mozambique
MM|MMR|Myanmar|Burma
NA|NAM|Namibia
NR|NRU|Nauru
NP|NPL|Nepal
NL|NLD|Netherlands|The Netherlands|Holland
NC|NCL|New Caledonia
NZ|NZL|New Zealand
NI|NIC|Nicaragua
NE|NER|Niger
NG|NGA|Nigeria
NU|NIU|Niue
NF|NFK|Norfolk Island
MP|MNP|Northern Mariana Islands
NO|NOR|Norway
OM|OMN|Oman
PK|PAK|Pakistan
PW|PLW|Palau
PS|PSE|Palestine|Palestine, State of|Palestinian Territory, Occupied
PA|PAN|Panama
PG|PNG|Papua New Guinea
PY|PRY|Paraguay
PE|PER|Peru
PH|PHL|Philippines
PN|PCN|Pitcairn
PL|POL|Poland
PT|PRT|Portugal
PR|PRI|Puerto Rico
QA|QAT|Qatar
RE|REU|Reunion
RO|ROU|Romania
RU|RUS|Russian Federation|Russia
RW|RWA|Rwanda
SH|SHN|Saint Helena
KN|KNA|Saint Kitts and Nevis
LC|LCA|Saint Lucia
PM|SPM|Saint Pierre and Miquelon
VC|VCT|Saint Vincent and the Grenadines
WS|WSM|Samoa
SM|SMR|San Marino
ST|STP|Sao Tome and Principe
SA|SAU|Saudi Arabia
SN|SEN|Senegal
RS|SRB|Serbia
SC|SYC|Seychelles
SL|SLE|Sierra Leone
SG|SGP|Singapore
SJ|SJM|Svalbard and Jan Mayen
SK|SVK|Slovakia
SI|SVN|Slovenia
SB|SLB|Solomon Islands
SO|SOM|Somalia
ZA|ZAF|South Africa
SS|SSD|South Sudan
ES|ESP|Spain
LK|LKA|Sri Lanka
SD|SDN|Sudan
SR|SUR|Suriname
SE|SWE|Sweden
CH|CHE|Switzerland
SX|SXM|Sint Maarten (Dutch part)|Sint Maarten
SY|SYR|Syria|Syrian Arab Republic
TW|TWN|Taiwan|Taiwan, Province of China
TJ|TJK|Tajikistan
TZ|TZA|Tanzania|Tanzania, United Republic of
TH|THA|Thailand
TL|TLS|Timor-Leste|East Timor
TF|ATF|French Southern Territories
TG|TGO|Togo
TK|TKL|Tokelau
TO|TON|Tonga
TT|TTO|Trinidad and Tobago
TN|TUN|Tunisia
TR|TUR|Turkey|Turkiye
TM|TKM|Turkmenistan
TC|TCA|Turks and Caicos Islands
TV|TUV|Tuvalu
UG|UGA|Uganda
UA|UKR|Ukraine
AE|ARE|United Arab Emirates|UAE
GB|GBR|United Kingdom|UK|Great Britain|Britain
US|USA|United States|United States of America|America|U.S.A.|U.S.
UM|UMI|United States Minor Outlying Islands
UY|URY|Uruguay
UZ|UZB|Uzbekistan
VU|VUT|Vanuatu
VE|VEN|Venezuela|Venezuela, Bolivarian Republic of
VN|VNM|Vietnam|Viet Nam
VG|VGB|Virgin Islands, British|British Virgin Islands
VI|VIR|Virgin Islands, U.S.|US Virgin Islands
WF|WLF|Wallis and Futuna
EH|ESH|Western Sahara
YE|YEM|Yemen
ZM|ZMB|Zambia
ZW|ZWE|Zimbabwe
"""

# postal | stored spelling
_US_STATES = """
AL|Alabama
AK|Alaska
AZ|Arizona
AR|Arkansas
CA|California
CO|Colorado
CT|Connecticut
DE|Delaware
DC|District of Columbia
FL|Florida
GA|Georgia
HI|Hawaii
ID|Idaho
IL|Illinois
IN|Indiana
IA|Iowa
KS|Kansas
KY|Kentucky
LA|Louisiana
ME|Maine
MD|Maryland
MA|Massachusetts
MI|Michigan
MN|Minnesota
MS|Mississippi
MO|Missouri
MT|Montana
NE|Nebraska
NV|Nevada
NH|New Hampshire
NJ|New Jersey
NM|New Mexico
NY|New York
NC|North Carolina
ND|North Dakota
OH|Ohio
OK|Oklahoma
OR|Oregon
PA|Pennsylvania
RI|Rhode Island
SC|South Carolina
SD|South Dakota
TN|Tennessee
TX|Texas
UT|Utah
VT|Vermont
VA|Virginia
WA|Washington
WV|West Virginia
WI|Wisconsin
WY|Wyoming
"""


def _entities() -> list[tuple[tuple[str, ...], tuple[str, ...]]]:
    """``(aliases, stored spellings)`` for every line of both tables."""
    out: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    for table, names_from in ((_COUNTRIES, 2), (_US_STATES, 1)):
        for line in table.strip().splitlines():
            fields = tuple(f.strip() for f in line.split("|"))
            # Names first, then the codes: a column may store either, and the
            # fuller spelling is the likelier one to be the intended value.
            out.append((fields, fields[names_from:] + fields[:names_from]))
    return out


def _build() -> dict[str, list[tuple[str, ...]]]:
    by_alias: dict[str, list[tuple[str, ...]]] = {}
    for fields, names in _entities():
        for alias in {normalize_value(f) for f in fields}:
            if alias and names not in by_alias.setdefault(alias, []):
                by_alias[alias].append(names)
    return by_alias


_BY_ALIAS = _build()


def builtin_candidates(alias: str) -> list[tuple[str, ...]]:
    """The entities *alias* may stand for, each as its stored spellings.

    *alias* is normalised here. Empty when it is not a known alias; more than
    one entity when it is ambiguous (``CA``, ``IN``, ``DE``).
    """
    return list(_BY_ALIAS.get(normalize_value(alias), []))
