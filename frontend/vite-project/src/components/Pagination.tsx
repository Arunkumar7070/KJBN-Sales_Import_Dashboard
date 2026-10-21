/** Page numbers to show: first, last, and the current page's neighbours. */
function pageWindow(page: number, pages: number): (number | "gap")[] {
  const wanted = new Set([1, pages, page - 1, page, page + 1].filter((p) => p >= 1 && p <= pages));
  const sorted = [...wanted].sort((a, b) => a - b);
  const result: (number | "gap")[] = [];
  sorted.forEach((value, index) => {
    if (index > 0 && value - sorted[index - 1] > 1) result.push("gap");
    result.push(value);
  });
  return result;
}

const pageButton = "rounded px-2 py-1 text-[11px] font-semibold tracking-[0.03em] transition-colors";
const idleButton = `${pageButton} bg-surface-container-lowest text-on-surface hover:bg-surface-container disabled:cursor-not-allowed disabled:text-on-surface-variant disabled:opacity-50 disabled:hover:bg-surface-container-lowest`;

export default function Pagination({
  page,
  pages,
  onPage,
}: {
  page: number;
  pages: number;
  onPage: (page: number) => void;
}) {
  const lastPage = Math.max(1, pages);
  return (
    <nav className="flex items-center gap-1" aria-label="Pagination">
      <button type="button" className={idleButton} disabled={page <= 1} onClick={() => onPage(page - 1)}>
        Previous
      </button>
      {pageWindow(page, lastPage).map((item, index) =>
        item === "gap" ? (
          <span key={`gap-${index}`} className="px-1 text-xs text-on-surface-variant">
            …
          </span>
        ) : (
          <button
            key={item}
            type="button"
            onClick={() => onPage(item)}
            aria-current={item === page ? "page" : undefined}
            className={`min-w-6 font-mono ${item === page ? `${pageButton} bg-primary text-on-primary` : idleButton}`}
          >
            {item}
          </button>
        ),
      )}
      <button type="button" className={idleButton} disabled={page >= lastPage} onClick={() => onPage(page + 1)}>
        Next
      </button>
    </nav>
  );
}
