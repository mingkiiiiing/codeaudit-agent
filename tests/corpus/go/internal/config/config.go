// Package config 应用配置：干净反例；常量口径样例。
// Go 无全大写惯例：const 一律记 constant，导出与否按首字母大小写（本文件
// PortHTTP 导出 / portDebug 未导出，kind 同为 constant）。
package config

// MaxRetries 单行 const 形态。
const MaxRetries = 3

// 圆括号块 const 形态（导出/未导出各一）。
const (
	PortHTTP  = 8080
	portDebug = 9090
)

// Describe 返回服务说明（跨包调用的目标符号）。
func Describe() string {
	return "audit-service"
}

// Endpoint 组合端点地址。
func Endpoint(host string) string {
	return host + ":" + itoa(PortHTTP)
}

// itoa 同包内未导出助手（同文件调用不建边）。
func itoa(n int) string {
	digits := "0123456789"
	if n < 0 || n > 9 {
		return "?"
	}
	return digits[n : n+1]
}
