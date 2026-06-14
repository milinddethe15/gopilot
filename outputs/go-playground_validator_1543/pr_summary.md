# feat: add dns_label validator

## Summary
Added a baked-in `dns_label` validator for single RFC 1123 DNS labels. The new validator rejects dots while enforcing alphanumeric/hyphen characters, alphanumeric start and end, and 1–63 character length.

## Changes
- Added `dns_label` to `bakedInValidators` in `baked_in.go`
- Implemented `isDnsRFC1123LabelFormat` in `baked_in.go`
- Added `dnsRegexStringRFC1123Label` and lazy regex compilation in `regexes.go`
- Documented `dns_label` usage and constraints in `doc.go`
- Added table-driven coverage for valid and invalid DNS labels in `validator_test.go`

## Testing
- Added `TestDNSLabelFormatValidation` covering valid single labels, dotted hostnames, invalid characters, bad hyphen placement, empty values, and 64-character labels
- Existing validator test suite continues to cover unchanged hostname and RFC 1035 label behavior

Closes #1543