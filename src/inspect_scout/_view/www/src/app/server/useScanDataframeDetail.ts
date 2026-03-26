import { skipToken } from "@tanstack/react-query";

import { useApi } from "../../state/store";
import { AsyncData } from "../../utils/asyncData";
import { useAsyncDataFromQuery } from "../../utils/asyncDataFromQuery";
import { ScanResultDetail } from "../../api/api";

type ScanDataframeDetailParams = {
  scansDir: string;
  scanPath: string;
  scanner: string;
  uuid: string;
};

export const useScanDataframeDetail = (
  params: ScanDataframeDetailParams | typeof skipToken
): AsyncData<ScanResultDetail> => {
  const api = useApi();

  return useAsyncDataFromQuery({
    queryKey:
      params === skipToken
        ? [skipToken]
        : ["scanDataframeDetail", params, "scans-inv"],
    queryFn:
      params === skipToken
        ? skipToken
        : () =>
            api.getScannerDataframeDetail(
              params.scansDir,
              params.scanPath,
              params.scanner,
              params.uuid
            ),
    staleTime: Infinity,
  });
};
