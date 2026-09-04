import { OcrProgressBar, type OcrProgressState } from "@/components/OcrProgressBar";

type Props = {
  progress: OcrProgressState | null;
  className?: string;
};

export function OperationProgress({ progress, className }: Props) {
  return <OcrProgressBar progress={progress} className={className} compact />;
}
