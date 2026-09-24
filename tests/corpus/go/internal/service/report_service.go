// Package service 报表服务：干净反例。
// fmt.Sprintf 用于报表标题（格式串无 SQL 关键字）——SQL 规则的反例样例。
package service

import (
	"fmt"

	"internal/util"
)

// ReportService 报表业务逻辑。
type ReportService struct {
	title string
}

// NewReportService 构造。
func NewReportService(title string) *ReportService {
	return &ReportService{title: title}
}

// Title 拼接报表标题（非 SQL 字符串拼接，反例）。
func (r *ReportService) Title(part string) string {
	return r.title + "-" + part
}

// Summarize 跨包解析正例：util.Summarize。
func (r *ReportService) Summarize(words []string) string {
	return fmt.Sprintf("%d 条记录", len(words)) + ": " + util.Summarize(words)
}
