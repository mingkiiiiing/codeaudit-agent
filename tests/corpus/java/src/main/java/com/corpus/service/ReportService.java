package com.corpus.service;

import java.util.ArrayList;
import java.util.List;

/** SQL 反例：全程参数化查询，拼接只发生在非 SQL 的展示文案上（不应命中）。 */
public class ReportService {

    public List<String> daily(int limit) {
        List<String> rows = new ArrayList<String>();
        String template = "SELECT metric, value FROM report_daily WHERE day = ?";
        rows.add(load(template, limit));
        rows.add("fetched " + limit + " rows");
        return rows;
    }

    private String load(String sql, int limit) {
        return sql + "#" + limit;
    }
}
