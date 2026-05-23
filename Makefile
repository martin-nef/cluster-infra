.PHONY: verify deploy

verify:
	cd ansible && ansible-playbook playbook.yml --syntax-check
	cd ansible && ansible-inventory --list >/dev/null

deploy:
	cd ansible && ansible-playbook playbook.yml

decrypt:
	./decrypt.sh

encrypt:
	./encrypt.sh

