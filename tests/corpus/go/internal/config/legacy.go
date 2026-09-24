// Package config 历史遗留凭据：硬编码密钥正例（const / var 两种声明形态）。
package config

// LegacyAPIKey 高熵 API key（const 形态正例）。
const LegacyAPIKey = "aX9kQ2vL8mN4pR7sT5uW3yZ0"

// dbPassword 口令家族（var 形态正例，低熵自然词放低闸门口径）。
var dbPassword = "mysupersecretkey"

// Placeholder 占位值（反例：低熵 changeme 不命中）。
const Placeholder = "changeme"

// Bucket 普通资源名（反例：无敏感词不命中）。
const Bucket = "prod-assets-bucket-name"

// MigrateLegacy 用遗留凭据连接（调用点样例）。
func MigrateLegacy() string {
	return "connected"
}
