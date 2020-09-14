const { VarAllocator } = require('../var_allocator.js')

const getChildren = async (dg_client, parentUid, childrenFilters) => {
    const varAlloc = new VarAllocator();
    
    varAlloc.alloc(childrenFilters.pid, 'int');
    varAlloc.alloc(childrenFilters.processName, 'string');

    const varTypes = varTypeList(varAlloc);
    const filter = generateFilter(varAlloc);
    const varListArray = Array.from(varAlloc.vars.keys());
    
    if (varListArray.indexOf('uid') === -1) {
        varListArray.push('uid');
    }
    
    if (varListArray.indexOf('node_key') === -1) {
        varListArray.push('node_key');
    }
    
    const varList = varListArray.join(", ");
    
    const query = `
        query process(${varTypes})
        {
            process(func: uid(${parentUid}))
            {
                children  @filter(
                    ${filter}
                ) {
                    ${varList}
                }
        
            }
        }
    `;

    const txn = dg_client.newTxn();

    try {
        const res = await txn.queryWithVars(query, reverseMap(varAlloc.vars));
        const parent = res.getJson()['process'][0];

        if (!parent) {
            return []
        }

        return parent['children'] || [];
    } 
    finally {
        await txn.discard();
    }
}

module.exports = {
    getChildren
}