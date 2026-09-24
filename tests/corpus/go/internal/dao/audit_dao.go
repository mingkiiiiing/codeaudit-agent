// Package dao 审计日志访问：SQL 注入正例（+ 拼接进 db.Query 执行）。
package dao

import (
	"fmt"

	"internal/model"
)

// AuditDAO 审计日志表访问。
type AuditDAO struct {
	source string
}

// NewAuditDAO 构造。
func NewAuditDAO(source string) *AuditDAO {
	return &AuditDAO{source: source}
}

// Search 按关键词检索审计日志（SQL 注入正例：第 + 拼接形态）。
func (a *AuditDAO) Search(term string, query QueryFn) {
	rows := query("SELECT * FROM audit_logs WHERE keyword='" + term + "'")
	item := model.FromRow(rows)
	fmt.Println(item)
}

// QueryFn 外部注入的执行函数（避免引入真实 sql 包依赖，形态等价）。
type QueryFn func(sql string) string
