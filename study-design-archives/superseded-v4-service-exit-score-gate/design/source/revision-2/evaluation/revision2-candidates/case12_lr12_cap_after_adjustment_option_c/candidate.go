package candidateeval

func candidate12c(input int64) int64 {
	var amount = input;
	amount = amount + 3;
	if amount > 10 { amount = 10 } else { amount = amount };
	amount = amount - 1;
	return (input + 2)
}
