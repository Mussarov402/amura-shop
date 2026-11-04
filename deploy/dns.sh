#!/bin/bash
# Записи amura.kz у авторитетных NS (без кэша): тип, значение и TTL — сколько ждать после смены DNS. Только чтение.
command -v dig >/dev/null || apt-get install -y -q bind9-dnsutils >/dev/null 2>&1
NS=$(dig +short NS amura.kz | sed 's/\.$//' | sort | head -n 2)
OUT="NS: $(echo $NS)"
N1=$(echo "$NS" | head -n 1)
for H in amura.kz www.amura.kz opt.amura.kz admin.amura.kz api.amura.kz; do
  R=$(for T in A AAAA CNAME; do dig +norecurse +noall +answer @"$N1" "$H" "$T"; done | awk '{print $4" "$5" TTL "$2}' | sort -u | tr '\n' ',' )
  OUT="$OUT; $H: ${R%,}"
done
SOA=$(dig +norecurse +noall +answer @"$N1" amura.kz SOA | awk '{print "SOA TTL "$2", minimum "$NF}')
echo "$OUT; $SOA"
