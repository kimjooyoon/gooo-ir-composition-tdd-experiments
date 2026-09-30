package bodycodegen

//gooo:generated:start id="bodycodegen://activity/c12" kind="activity"
func C12(input int64) int64 {
	var amount = input
	amount = amount + 3
	if amount > 10 {
		amount = 10
	} else {
		amount = amount
	}
	amount = amount - 1
	return amount
}

//gooo:generated:end id="bodycodegen://activity/c12" kind="activity"
