import type { ReactNode } from "react";
import { ArrowRight, type LucideIcon } from "lucide-react";

/** The three beats of a handoff: where it starts, what fabri does, what lands back. */
export type Handoff = [string, string, string];

/**
 * One integration on the Settings surface.
 *
 * The card leads with the handoff — where a task starts, what the agency does,
 * and what comes back — because that mechanism is the reason to connect at all.
 * Signed-out visitors see all of it; only the connect action is gated, so the
 * integrations read as product surface rather than a hidden account feature.
 */
export default function IntegrationSection({
  name,
  blurb,
  icon: Icon,
  hue,
  handoff,
  locked,
  onRequireAuth,
  connect,
  children,
}: {
  name: string;
  blurb: string;
  icon: LucideIcon;
  /** The provider's hue, used for its mark and handoff line only. */
  hue: string;
  handoff: Handoff;
  locked: boolean;
  onRequireAuth: () => void;
  /** The signed-in connect control — a link into the provider's install flow. */
  connect: ReactNode;
  /** Connected accounts and any status/error banners. */
  children?: ReactNode;
}) {
  return (
    <section className="integration" style={{ ["--brand" as string]: hue }}>
      <div className="integration__head">
        <span className="integration__mark" aria-hidden>
          <Icon size={17} strokeWidth={1.8} />
        </span>
        <h3 className="integration__name">{name}</h3>
      </div>

      <p className="integration__blurb">{blurb}</p>

      <ol className="handoff" aria-label={`How ${name} works with fabri`}>
        {handoff.map((step, i) => (
          <li className="handoff__step" key={step}>
            <span className="handoff__dot" aria-hidden />
            <span className="handoff__label">{step}</span>
            {i < handoff.length - 1 && <span className="handoff__line" aria-hidden />}
          </li>
        ))}
      </ol>

      <div className="integration__foot">
        {locked ? (
          <button className="integration__cta" onClick={onRequireAuth}>
            Sign in to connect {name}
            <ArrowRight size={13} strokeWidth={2} aria-hidden />
          </button>
        ) : (
          <>
            <div className="integration__connect">{connect}</div>
            {children}
          </>
        )}
      </div>
    </section>
  );
}
