# Gemini Burmese Language Review

Route: `ag/gemini-3.7-flash-high` through the user-authorized external 9Router endpoint.

## Candidate audit

The reviewer scored the short candidate 8/10. It approved `မှန်းသုံးစရာ မလိုတော့ဘူး` and correctly identified `ကြိုသိပေးမယ်` as awkward. The final uses `ကြိုသတိပေး`.

The reviewer suggested `Used / Remaining data` and `ကြို alert ပေးမယ်`. Both were rejected because they are unnecessarily English-heavy and `data` can be confused with mobile-data allowance.

## Final exact audit

The exact visible lines and caption received 9/10 with minor edits.

Adopted:

- removed the duplicate Bot handle below the hero icon;
- simplified the quota-reached sentence to `Quota ပြည့်သွားရင် Key ရပ်သွားကြောင်းလည်း သီးခြားအသိပေးပါတယ်။`

Rejected:

- `ရောက်တိုင်း`, because a maintenance pass that crosses several thresholds can queue only the deepest newly reached warning;
- `ချက်ချင်း`, because immediate delivery has not been production-verified;
- formal `နှင့်` in the customer-facing list, where natural `နဲ့` better matches the selected register.

## User-sample R4 review

After the user supplied a conversational meeting scenario, Gemini ranked `Meeting ဝင်နေတုန်း / Outline VPN Quota ကုန်သွားဖူးလား?` first: 9/10 naturalness, 9/10 catchiness, and 10/10 audience fit. The final visual uses the existing `OUTLINE VPN QUOTA` context label and shortens the display hook to `Meeting ဝင်နေတုန်း / Quota ကုန်သွားဖူးလား?`.

The exact R4 image lines and caption then received 9/10 and a language-ready verdict. Adopted corrections:

- `Quota ပြည့်သွားရင် Key ရပ်သွားကြောင်းလည်း သီးခြားအသိပေးပါတယ်။`
- `လက်ကျန်ပမာဏနဲ့ ရာခိုင်နှုန်း`, retaining natural `နဲ့` instead of the reviewer’s more formal `နှင့်`.

Rejected from the candidate response:

- reducing the payment line to only KPay and WavePay;
- an invented trial CTA;
- a placeholder Bot handle.
