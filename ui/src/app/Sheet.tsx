import { useEffect, useRef, type ReactNode } from "react";
import { X } from "lucide-react";

/** A panel that slides in from the right over the page: detail for one thing,
 *  without leaving the list it came from. Escape and the close button shut it. */
export function Sheet({
  title,
  sub,
  onClose,
  children,
  foot,
  label,
}: {
  title: ReactNode;
  sub?: ReactNode;
  onClose: () => void;
  children: ReactNode;
  foot?: ReactNode;
  label: string;
}) {
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    const before = document.activeElement as HTMLElement | null;
    box.current?.focus();
    return () => {
      document.removeEventListener("keydown", onKey);
      before?.focus?.();
    };
  }, [onClose]);

  return (
    <>
      <div className="sheet-scrim" onClick={onClose} aria-hidden="true" />
      <div className="sheet" role="dialog" aria-modal="true" aria-label={label} tabIndex={-1} ref={box}>
        <div className="sheet-head">
          <div>
            <div className="ttl">{title}</div>
            {sub && <div className="sub">{sub}</div>}
          </div>
          <button type="button" className="btn quiet sm icon-only" aria-label="Close" onClick={onClose}>
            <X />
          </button>
        </div>
        <div className="sheet-body">{children}</div>
        {foot && <div className="sheet-foot">{foot}</div>}
      </div>
    </>
  );
}
