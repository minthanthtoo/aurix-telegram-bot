# Payment QR assets

These customer-facing cards contain only the payment method branding and the
merchant QR. Visible account names, phone numbers, device chrome, and support
numbers were removed from the supplied screenshots.

The QR payload is still payment-routing data and may reveal the recipient name
inside the selected wallet application. Treat these files as payment
credentials: replace them immediately if the receiving account changes, and
verify every replacement by decoding both the source and final card before
deployment.

Display order is fixed to KBZPay, WavePay, AYA Pay, UABPay, and CB Pay.
