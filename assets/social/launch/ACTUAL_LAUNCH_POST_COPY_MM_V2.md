# AuriX အများပြည်သူသုံး စတင်မိတ်ဆက်ပို့စ် — Offer V2

Version: MM 2.0  
Status: Creative approved terms; implementation verification required before publication

## Offer interpretation

- လူတိုင်းအတွက် နေ့စဉ် `300 MB` အခမဲ့။
- လူတိုင်းအတွက် လစဉ် `3 GB` အခမဲ့ ထပ်မံရရှိမှု။
- `50 GB / 30 days` — `3,000 Kyats`.
- `100 GB / 30 days` — `6,000 Kyats`.

“3 GB per month too” ကို နေ့စဉ် 300 MB အပြင် လူတိုင်းအတွက် လစဉ် 3 GB အခမဲ့ ထပ်မံရရှိခြင်းဟု အဓိပ္ပာယ်ဖွင့်ထားပါတယ်။

## Strategy lock

- Core promise: အခမဲ့မှ အခပေးအထိ ပမာဏနှင့် ဈေးနှုန်းရှင်းလင်းသော VPN ရွေးချယ်မှု။
- Product memory: `300 MB daily + 3 GB monthly`; paid plans start at `3,000 Kyats`.
- CTA: Telegram မှာ စတင်ရန်။
- Hero-post rule: offer ladder တစ်ခုတည်းကို ပြပြီး payment mechanics ကို caption ထဲတွင်သာ ထားရန်။

## ပုံပေါ်စာသား

Launch label:

> AuriX စတင်မိတ်ဆက်ပါပြီ

Headline:

> အသုံးပြုခွင့် ရှင်းလင်း။  
> စတင်ရတာ လွယ်ကူ။

Free card:

> လူတိုင်းအတွက် အခမဲ့  
> နေ့စဉ် 300 MB  
> လစဉ် 3 GB

Paid cards:

> 50 GB · 3,000 ကျပ်  
> 100 GB · 6,000 ကျပ်

CTA:

> Telegram မှာ စတင်ပါ

Terms:

> Paid plans · ရက် 30 · ရရှိနိုင်မှုအပေါ် မူတည်သည်

## Facebook caption

AuriX စတင်မိတ်ဆက်ပါပြီ။

VPN သုံးဖို့ ရွေးချယ်ရာမှာ ဒေတာပမာဏ၊ အသုံးပြုနိုင်တဲ့ကာလနဲ့ ဈေးနှုန်းကို ကြိုတင်ရှင်းရှင်းလင်းလင်း သိသင့်ပါတယ်။ AuriX ကို Telegram ကနေ အလွယ်တကူ စတင်နိုင်ပါပြီ။

**လူတိုင်းအတွက် အခမဲ့**

- နေ့စဉ် 300 MB
- လစဉ် 3 GB

**အခပေးအစီအစဉ်များ**

- 50 GB · ရက် 30 · 3,000 ကျပ်
- 100 GB · ရက် 30 · 6,000 ကျပ်

စတင်အသုံးပြုဖို့ Telegram bot ကိုဖွင့်ပြီး `/start` ကိုနှိပ်ပါ။ အခက်အခဲရှိရင် AuriX မှ လူကိုယ်တိုင် ကူညီပေးပါမယ်။

အသုံးပြုခွင့်နှင့် အစီအစဉ်များသည် ဝန်ဆောင်မှုပေးနိုင်မှုနှင့် သတ်မှတ်ချက်များအပေါ် မူတည်ပါတယ်။

`#AuriX #VPNMyanmar #MyanmarTech #TelegramBot`

## Publication gate

လက်ရှိ repository implementation သည် ဤ V2 offer နှင့် မကိုက်ညီသေးပါ။ လက်ရှိ code က 200 MiB rolling-24-hour claim၊ one-time 3 GiB trial နှင့် active 50 GB plan ကိုသာ ပံ့ပိုးထားပြီး 100 GB plan ကို disabled လုပ်ထားပါတယ်။ အောက်ပါအချက်များပြီးမှ ဤ post ကို public ထုတ်ဝေရပါမယ်။

1. Daily allowance ကို 300 MB သို့ update လုပ်ပြီး live claim test အောင်မြင်ရန်။
2. Monthly 3 GB entitlement semantics၊ reset date နှင့် abuse controls ကို implementation နှင့် terms တွင် တိတိကျကျ သတ်မှတ်ရန်။
3. 100 GB / 6,000 ကျပ် plan ကို enable လုပ်ပြီး order, payment review, provisioning နှင့် quota test ပြီးရန်။
4. 50 GB နှင့် 100 GB နှစ်မျိုးစလုံးအတွက် capacity နှင့် support ownership အတည်ပြုရန်။

