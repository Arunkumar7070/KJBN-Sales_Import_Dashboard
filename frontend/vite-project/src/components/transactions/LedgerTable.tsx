import { useEffect, useState } from "react";
import type { PaymentMode, Transaction } from "../../api/types";
import { formatINR, formatIstDateTime } from "../../lib/format";
import { Icon } from "../ui";

const PAYMENT_BADGE: Record<PaymentMode, { icon: string; className: string }> = {
  UPI: { icon: "account_balance", className: "bg-secondary-container text-on-secondary-fixed" },
  CARD: { icon: "credit_card", className: "bg-primary-fixed text-on-primary-fixed" },
  CASH: { icon: "payments", className: "bg-on-tertiary-container text-tertiary" },
};

const HEADERS: [string, string][] = [
  ["Txn ID", "px-3"],
  ["Store", "px-2"],
  ["Timestamp (IST)", "px-3"],
  ["SKU & Product Details", "px-3 min-w-[220px]"],
  ["Qty", "px-2 text-center"],
  ["Unit Price", "px-3 text-right"],
  ["Discount", "px-3 text-right"],
  ["Line Total", "px-3 text-right"],
  ["Payment", "px-3 text-center"],
  ["Original Txn ID", "px-3"],
];

type CopyState = "idle" | "copied" | "failed";

function CopyIdButton({ id, isReturn }: { id: string; isReturn: boolean }) {
  const [state, setState] = useState<CopyState>("idle");

  useEffect(() => {
    if (state === "idle") return;
    const timer = window.setTimeout(() => setState("idle"), 1500);
    return () => window.clearTimeout(timer);
  }, [state]);

  function copy() {
    // navigator.clipboard is undefined outside secure contexts (e.g. a plain-http LAN host).
    const write = navigator.clipboard?.writeText(id) ?? Promise.reject(new Error("Clipboard unavailable"));
    write.then(
      () => setState("copied"),
      () => setState("failed"),
    );
  }

  const icon = { idle: "content_copy", copied: "check", failed: "error" }[state];
  const title = { idle: "Copy Txn ID", copied: "Copied", failed: "Copy failed" }[state];
  return (
    <button
      type="button"
      onClick={copy}
      title={title}
      aria-label={`Copy transaction ID ${id}`}
      className={`flex transition-colors ${
        isReturn ? "text-error/70 hover:text-error" : "text-on-surface-variant/50 hover:text-primary"
      }`}
    >
      <Icon name={icon} className="text-[14px]" />
    </button>
  );
}

function Row({ transaction }: { transaction: Transaction }) {
  const isReturn = transaction.is_return;
  const lineTotal = transaction.line_total.paise;
  const payment = PAYMENT_BADGE[transaction.payment_mode];
  return (
    <tr
      className={`h-11 transition-colors ${
        isReturn ? "bg-error-container/30 hover:bg-error-container/50" : "hover:bg-surface-container-low/60"
      }`}
    >
      <td className="px-3 font-mono text-xs">
        <div className="flex items-center gap-1.5">
          <span className={isReturn ? "font-bold text-error" : "font-semibold text-primary"}>
            {transaction.transaction_id}
          </span>
          {isReturn && (
            <span className="rounded bg-error px-1 font-sans text-[9px] font-bold uppercase tracking-tight text-on-error">
              RET
            </span>
          )}
          <CopyIdButton id={transaction.transaction_id} isReturn={isReturn} />
        </div>
      </td>
      <td className="px-2">
        <span
          className={`rounded px-2 py-0.5 font-mono text-xs ${
            isReturn ? "bg-error-container font-bold text-error" : "bg-surface-container-high font-semibold text-on-surface"
          }`}
        >
          {transaction.store_code}
        </span>
      </td>
      <td className={`px-3 font-mono text-xs ${isReturn ? "font-medium text-error" : "text-on-surface-variant"}`}>
        {formatIstDateTime(transaction.sold_at)}
      </td>
      <td className="px-3">
        <div className="flex items-center gap-2">
          <span className={`font-mono text-xs ${isReturn ? "font-bold text-error" : "font-semibold text-on-surface"}`}>
            {transaction.sku}
          </span>
          <span className={isReturn ? "text-error/40" : "text-outline-variant"}>•</span>
          <span
            className={`max-w-[200px] truncate ${isReturn ? "font-medium text-error" : ""}`}
            title={transaction.product_name}
          >
            {transaction.product_name}
          </span>
        </div>
      </td>
      <td className="px-2 text-center">
        {isReturn ? (
          <span className="inline-flex items-center justify-center gap-0.5 rounded-full bg-error px-2.5 py-0.5 font-mono text-xs font-bold text-on-error shadow-sm">
            <Icon name="keyboard_return" className="text-[12px]" />
            {transaction.quantity}
          </span>
        ) : (
          <span className="inline-flex items-center justify-center rounded-full bg-tertiary-fixed px-2 py-0.5 font-mono text-xs font-bold text-on-tertiary-fixed">
            +{transaction.quantity}
          </span>
        )}
      </td>
      <td className="px-3 text-right font-mono text-xs text-on-surface">{formatINR(transaction.unit_price.paise)}</td>
      <td className="px-3 text-right font-mono text-xs text-on-surface-variant">
        {formatINR(transaction.discount.paise)}
      </td>
      <td className={`px-3 text-right font-mono text-xs font-bold ${isReturn ? "text-error" : "text-on-surface"}`}>
        {lineTotal > 0 ? "+" : ""}
        {formatINR(lineTotal)}
      </td>
      <td className="px-3 text-center">
        <span
          className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-semibold ${payment.className}`}
        >
          <Icon name={payment.icon} className="text-[12px]" />
          {transaction.payment_mode}
        </span>
      </td>
      <td className="px-3 font-mono text-xs">
        {transaction.original_transaction_id ? (
          <span
            className="inline-flex items-center gap-1 rounded bg-surface-container-lowest px-1.5 py-0.5 font-semibold text-primary shadow-sm"
            title="The sale this return refunds"
          >
            <Icon name="link" className="text-[12px]" />
            {transaction.original_transaction_id}
          </span>
        ) : (
          <span className="text-outline-variant">—</span>
        )}
      </td>
    </tr>
  );
}

export default function LedgerTable({ transactions }: { transactions: Transaction[] }) {
  return (
    <div className="w-full overflow-x-auto">
      <table className="w-full whitespace-nowrap text-left">
        <thead>
          <tr className="h-10 bg-surface-container-low text-[11px] font-semibold uppercase tracking-wider text-on-surface-variant">
            {HEADERS.map(([label, className]) => (
              <th key={label} className={`font-semibold ${className}`}>
                {label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-surface-container-low text-xs text-on-surface">
          {transactions.map((transaction) => (
            <Row key={transaction.transaction_id} transaction={transaction} />
          ))}
        </tbody>
      </table>
    </div>
  );
}
