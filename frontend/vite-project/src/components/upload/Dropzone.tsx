import { useRef, useState, type DragEvent } from "react";
import { Icon } from "../ui";

export default function Dropzone({
  multiple,
  title,
  hint,
  disabled = false,
  onFiles,
  compact = false,
  icon = "cloud_upload",
}: {
  multiple: boolean;
  title: string;
  hint: React.ReactNode;
  disabled?: boolean;
  onFiles: (files: File[]) => void;
  compact?: boolean;
  icon?: string;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);

  function handleDrop(event: DragEvent) {
    event.preventDefault();
    setDragging(false);
    if (!disabled) onFiles(Array.from(event.dataTransfer.files));
  }

  return (
    <div
      role="button"
      tabIndex={disabled ? -1 : 0}
      aria-disabled={disabled}
      onClick={() => !disabled && inputRef.current?.click()}
      onKeyDown={(event) => {
        if (!disabled && (event.key === "Enter" || event.key === " ")) {
          event.preventDefault();
          inputRef.current?.click();
        }
      }}
      onDragEnter={(event) => {
        event.preventDefault();
        if (!disabled) setDragging(true);
      }}
      onDragOver={(event) => event.preventDefault()}
      onDragLeave={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragging(false);
      }}
      onDrop={handleDrop}
      className={`flex flex-col items-center justify-center gap-2 rounded-xl border text-center outline-none transition-all focus-visible:ring-2 focus-visible:ring-primary-container ${
        compact ? "p-4" : "p-6"
      } ${
        disabled
          ? "cursor-not-allowed border-dashed border-outline-variant bg-surface-container-low opacity-60"
          : dragging
            ? "cursor-copy border-solid border-primary-container bg-primary-fixed/40"
            : "cursor-pointer border-dashed border-outline-variant bg-surface-container-low hover:bg-surface-container"
      }`}
    >
      <input
        ref={inputRef}
        type="file"
        accept=".csv,text/csv"
        multiple={multiple}
        hidden
        onChange={(event) => {
          onFiles(Array.from(event.target.files ?? []));
          event.target.value = ""; // Allow picking the same file again later.
        }}
      />
      <div
        className={`flex items-center justify-center rounded-full bg-surface-container-lowest text-primary shadow-sm ${
          compact ? "h-10 w-10" : "h-12 w-12"
        }`}
      >
        <Icon name={icon} className={compact ? "text-[20px]" : "text-[24px]"} />
      </div>
      <div className="flex flex-col items-center gap-0.5">
        <p className="text-sm font-semibold tracking-[0.01em] text-on-surface">{title}</p>
        <p className="text-xs text-on-surface-variant">{hint}</p>
      </div>
    </div>
  );
}
