type Props = {
  text?: string;
};

export function RevisedHpiPanel({ text }: Props) {
  if (!text?.trim()) {
    return (
      <div className="pr-el-empty" role="status">
        <span className="pr-el-empty__glyph" aria-hidden />
        <p className="pr-el-empty__title">No revised narrative returned</p>
        <p className="pr-el-empty__hint pr-microcopy">
          When present, revised narrative is generated from the source notes and normalized facts only, for documentation
          preview. It does not determine admission.
        </p>
      </div>
    );
  }

  return (
    <div className="pr-revised">
      <p className="pr-banner">
        Generated from source note + normalized facts only · not used for admission determination
      </p>
      <div className="pr-revised__note pr-scroll" tabIndex={0}>
        {text}
      </div>
    </div>
  );
}
