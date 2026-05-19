type Props = {
  originalHpi: string;
  erNote: string;
};

export function SourceNotesPanel({ originalHpi, erNote }: Props) {
  return (
    <div className="pr-split">
      <div className="pr-panel">
        <div className="pr-panel__label">Original HPI</div>
        <div className="pr-panel__text pr-scroll" tabIndex={0}>
          {originalHpi.trim() ? originalHpi : <span className="pr-muted">No text provided.</span>}
        </div>
      </div>
      <div className="pr-panel">
        <div className="pr-panel__label">ER note</div>
        <div className="pr-panel__text pr-scroll" tabIndex={0}>
          {erNote.trim() ? erNote : <span className="pr-muted">No text provided.</span>}
        </div>
      </div>
    </div>
  );
}
