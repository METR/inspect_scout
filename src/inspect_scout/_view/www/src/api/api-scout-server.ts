import { Scans, Status } from "../types";

import { NoPersistence, ScanApi } from "./api";
import { serverRequestApi } from "./request.ts";

export type HeaderProvider = () => Promise<Record<string, string>>;

export const apiScoutServer = (
  options: {
    apiBaseUrl?: string;
    headerProvider?: HeaderProvider;
    resultsDir?: string;
  } = {}
): ScanApi => {
  const { apiBaseUrl, headerProvider, resultsDir } = options;
  const requestApi = serverRequestApi(apiBaseUrl || "/api", headerProvider);
  return {
    getScan: async (scanLocation: string): Promise<Status> => {
      let query = `/scan/${encodeURIComponent(scanLocation)}`;
      if (resultsDir) {
        query += `?results_dir=${encodeURIComponent(resultsDir)}`;
      }
      return (await requestApi.fetchType<Status>("GET", query)).parsed;
    },
    getScans: async (): Promise<Scans> => {
      let query = "/scans?status_only=true";
      if (resultsDir) {
        query += `&results_dir=${encodeURIComponent(resultsDir)}`;
      }
      return (await requestApi.fetchType<Scans>("GET", query)).parsed;
    },
    getScannerDataframe: async (
      scanLocation: string,
      scanner: string
    ): Promise<ArrayBuffer> => {
      let query = `/scanner_df/${encodeURIComponent(
        scanLocation
      )}?scanner=${encodeURIComponent(scanner)}`;
      if (resultsDir) {
        query += `&results_dir=${encodeURIComponent(resultsDir)}`;
      }
      return await requestApi.fetchBytes("GET", query);
    },
    storage: NoPersistence,
  };
};
