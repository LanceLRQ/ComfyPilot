import { type ReactNode, useLayoutEffect, useRef } from "react";

export function SidePanel({
  id,
  title,
  open,
  dock = false,
  onClose,
  children,
}: {
  id: string;
  title: string;
  open: boolean;
  dock?: boolean;
  onClose: () => void;
  children: ReactNode;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const opener = useRef<HTMLElement | null>(null);
  useLayoutEffect(() => {
    const panel = dialog.current;
    if (!panel) return;
    const wide = window.matchMedia("(min-width: 1200px)");
    function update() {
      if (!panel) return;
      if (!open) {
        const inside = panel.contains(document.activeElement);
        panel.close();
        if (inside && opener.current?.checkVisibility())
          opener.current.focus({ preventScroll: true });
        return;
      }
      if (!panel.open) opener.current = document.activeElement as HTMLElement | null;
      const modal = !dock || !wide.matches;
      if (!modal) {
        const top = panel.parentElement?.getBoundingClientRect().top ?? 0;
        panel.style.setProperty("--dock-offset", `${Math.max(16, top + window.scrollY)}px`);
      }
      if (panel.open && panel.matches(":modal") === modal) return;
      const { scrollX, scrollY } = window;
      panel.close();
      if (modal) panel.showModal();
      else panel.show();
      panel.querySelector<HTMLElement>("textarea, h2")?.focus({ preventScroll: true });
      window.scrollTo(scrollX, scrollY);
    }
    update();
    wide.addEventListener("change", update);
    window.addEventListener("resize", update);
    return () => {
      wide.removeEventListener("change", update);
      window.removeEventListener("resize", update);
    };
  }, [open, dock]);
  return (
    <dialog
      ref={dialog}
      id={id}
      className="side-panel settings"
      data-dock={dock}
      aria-labelledby={`${id}-title`}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onKeyDown={(event) => {
        if (event.key === "Tab" && event.currentTarget.matches(":modal")) {
          const controls = Array.from(
            event.currentTarget.querySelectorAll<HTMLElement>(
              "button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), summary, [tabindex='0']",
            ),
          ).filter((item) => item.checkVisibility());
          const current = controls.indexOf(document.activeElement as HTMLElement);
          if (event.shiftKey ? current <= 0 : current === controls.length - 1 || current === -1) {
            event.preventDefault();
            controls[event.shiftKey ? controls.length - 1 : 0]?.focus();
          }
        }
        if (event.key === "Escape") {
          event.preventDefault();
          onClose();
        }
      }}
    >
      <header className="panel-header">
        <h2 id={`${id}-title`} tabIndex={-1}>
          {title}
        </h2>
        <button type="button" onClick={onClose} aria-label={`关闭${title}`}>
          关闭
        </button>
      </header>
      <div className="panel-body">{children}</div>
    </dialog>
  );
}
