package bodycodegen

//gooo:generated:start id="bodycodegen://activity/c11" kind="activity"
func C11(input int64) int64 {
	var score = input * 2
	var nonnegative = score >= 0
	if nonnegative {
		score = score + 1
	} else {
		score = 0
	}
	return (input*2 + 1)
}

//gooo:generated:end id="bodycodegen://activity/c11" kind="activity"
